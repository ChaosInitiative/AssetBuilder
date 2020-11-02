import cas.common.utilities as utilities
from cas.common.models import BuildEnvironment
from cas.common.buildsys.shared import BaseCompiler

import os
import shutil
import logging
import requests
import subprocess
import multiprocessing
from typing import List, Mapping
from pathlib import Path

import tqdm
import appdirs

DOCKER_IMAGE_TAG = "chaos-cas-steamrt:latest"
STEAMRT_REPO_URL = "https://repo.steampowered.com/steamrt-images-soldier/snapshots/latest-container-runtime-depot"
STEAMRT_IMAGE_URL = (
    STEAMRT_REPO_URL
    + "/com.valvesoftware.SteamRuntime.Sdk-amd64,i386-soldier-sysroot.tar.gz"
)
STEAMRT_IMAGE_SHA256 = (
    "4a77fbd1ba45286eedf0446ecfd7d42ab0cc53bc1bc0cecd53efd75ba040598f"
)


class BaseCompileEnvironment:
    """
    Base compilation environment
    """

    def __init__(self, env: BuildEnvironment, config: Mapping):
        self._env = env
        self._config = config
        self._env_config = config.environment.config
        self._env_vars = self._build_static_env()
        self._logger = logging.getLogger(self.__class__.__module__)

    def _build_static_env(self) -> dict:
        return {
            "CC": self._config.cc,
            "CXX": self._config.cxx,
            "JOBS": self._config.get("jobs", multiprocessing.cpu_count()),
            "PLATFORM": self._config.platform,
        }

    def _build_env(self, env: Mapping[str, str]) -> Mapping[str, str]:
        nenv = os.environ.copy()
        nenv.update(self._env_vars)
        nenv.update(env)

        return nenv

    def run(
        self, args: List[str], env: Mapping[str, str] = {}, path_suffix: str = None
    ) -> int:
        return NotImplementedError()


class NativeCompileEnvironment(BaseCompileEnvironment):
    """
    Compilation environment that executes commands in a native shell
    """

    def run(
        self, args: List[str], env: Mapping[str, str] = {}, path_suffix: str = None
    ) -> int:
        cwd = self._env.config.path.src.joinpath(path_suffix)
        return self._env.run_tool(args, env=self._build_env(env), cwd=cwd).returncode


class ChrootCompileEnvironment(BaseCompileEnvironment):
    """
    Compilation environment that executes commands using schroot
    """

    def run(
        self, args: List[str], env: Mapping[str, str] = {}, path_suffix: str = None
    ) -> int:
        cwd = self._env.config.path.src.joinpath(path_suffix)
        defargs = ["schroot", "-c", self._env_config.name, "--", "bash", "-c"]

        return self._env.run_subprocess(
            defargs.extend(args), env=self._build_env(env), cwd=cwd
        ).returncode


class DockerCompileEnvironment(BaseCompileEnvironment):
    """
    Compilation environment that executes commands using Docker
    """

    def _ensure_installed(self) -> bool:
        cache_folder = Path(appdirs.user_cache_dir("chaos_cas")).joinpath("docker")
        cache_folder.mkdir(parents=True, exist_ok=True)

        # check docker to see if the image already exists
        rebuild_image = False
        ret = self._env.run_subprocess(
            ["docker", "inspect", "--type=image", DOCKER_IMAGE_TAG],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if ret.returncode != 0:
            rebuild_image = True

        sysroot_file = cache_folder.joinpath("steamrt-sysroot.tar.gz")
        if sysroot_file.exists():
            sysroot_hash = utilities.hash_file_sha256(sysroot_file)
            if not sysroot_hash == STEAMRT_IMAGE_SHA256:
                self._logger.warning(
                    "SteamRT docker image hash does not match, redownloading!"
                )
                sysroot_file.unlink()

        if not sysroot_file.exists():
            response = requests.get(STEAMRT_IMAGE_URL, stream=True, timeout=1)
            with tqdm.tqdm(
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                total=int(response.headers["Content-Length"]),
                desc="Downloading SteamRT docker image",
            ) as progress:
                with open(sysroot_file, "wb") as f:
                    for chunk in response.iter_content(chunk_size=1024):
                        if chunk:
                            progress.update(len(chunk))
                            f.write(chunk)

            # verify the hash
            sysroot_hash = utilities.hash_file_sha256(sysroot_file)
            if not sysroot_hash == STEAMRT_IMAGE_SHA256:
                self._logger.error("Failed to verify the SteamRT docker image hash")
                self._logger.error(f"Wanted {STEAMRT_IMAGE_SHA256}")
                self._logger.error(f"Got {sysroot_hash}")
                sysroot_file.unlink()
                return False

            # ensure we rebuild the image as our file changed
            rebuild_image = True

        if rebuild_image:
            shutil.copyfile(
                Path(__file__).parent.joinpath("chaos-cas-steamrt.Dockerfile"),
                cache_folder.joinpath("Dockerfile"),
            )
            ret = self._env.run_subprocess(
                ["docker", "build", "--tag", DOCKER_IMAGE_TAG, "."], cwd=cache_folder
            )
            if not ret.returncode == 0:
                self._logger.error("Failed to build the SteamRT docker image")
                return False

        return True

    def run(
        self, args: List[str], env: Mapping[str, str] = {}, path_suffix: str = None
    ) -> int:
        if not self._ensure_installed():
            return 1

        root_path = self._env.config.path.root

        cwd = str(root_path.joinpath("src"))
        if path_suffix:
            cwd += f"/{path_suffix}"

        defargs = [
            "docker",
            "run",
            "--rm",
            "-i",
            "-v",
            f"{root_path}:{root_path}",
            "-w",
            cwd,
        ]

        nenv = self._env_vars.copy()
        nenv.update(env)

        for k, v in nenv.items():
            defargs.append("-e")
            defargs.append(f"{k}={v}")
        defargs.append(DOCKER_IMAGE_TAG)
        defargs.extend(args)
        self._logger.debug(defargs)

        return self._env.run_subprocess(defargs).returncode


class BaseDependency:
    """
    Represents an external third-party dependency that is not managed through VPC to build
    """

    def __init__(self, name: str, config: Mapping):
        self._name = name
        self._config = config

    def build(
        self, env: BaseCompileEnvironment, envvars: Mapping[str, str] = {}
    ) -> bool:
        return env.run(self._config.run, envvars, self._config.src) == 0


_compile_environments = {
    "native": NativeCompileEnvironment,
    "chroot": ChrootCompileEnvironment,
    "docker": DockerCompileEnvironment,
}


class PosixCompiler(BaseCompiler):
    """
    Generic POSIX compiler, used to build via generated makefiles
    """

    def __init__(self, env: BuildEnvironment, config: dict, platform: str):
        super().__init__(env, config.posix, platform)
        self._build_type = config.type
        self._compile_env = _compile_environments[self._config.environment.type](
            env, self._config
        )
        self._dependencies = {
            BaseDependency(k, v) for k, v in self._config.dependencies.items()
        }

    def _build_dependencies(self, clean: bool = False) -> bool:
        bstr = "cleaning" if clean else "building"
        envvars = {}

        if clean:
            envvars["CLEAN"] = "1"

        for dependency in self._dependencies:
            self._logger.info(f"{bstr} dependency {dependency._name}")
            if not dependency.build(self._compile_env, envvars):
                return False
        return True

    def _run_makefile(self, makefile: str, clean: bool = False) -> bool:
        jobs = self._config.get("jobs", multiprocessing.cpu_count())
        sanitizers = self._config.sanitizers

        args = ["make", "-f", makefile, f"-j{jobs}"]

        envvars = {
            "CFG": self._build_type,
            "ASAN": sanitizers.address,
            "UBSAN": sanitizers.behavior,
            "TSAN": sanitizers.threading,
            "NO_STRIP": False,
            "NO_DBG_INFO": False,
            "VALVE_NO_AUTO_P4": True,
        }

        self._compile_env.run(args, utilities.map_to_envvars(envvars))
        return True

    def clean(self, solution: str) -> bool:
        if not self._build_dependencies(True):
            self._logger.error("dependency clean failed; run with --verbose to see why")

        if not self._run_makefile(solution, True):
            self._logger.error("clean failed")

        return True

    def configure(self, solution: str) -> bool:
        if not super().configure(solution):
            return False

        if not self._build_dependencies():
            self._logger.error("dependency build failed; run with --verbose to see why")
            return False

        return True

    def build(self, solution: str) -> bool:
        if not self._run_makefile(f"{solution}.mak"):
            self._logger.error("build failed")
            return False
        return True
