import assetbuilder.utilities as utilities
from assetbuilder.config import ConfigurationManager

from typing import List, Set
from pathlib import Path
import os
import sys
import uuid
import logging
import subprocess


class BuildEnvironment():
    """
    Contains attributes about the current build environment
    """
    def __init__(self, path: str, config: dict):
        self.config = ConfigurationManager(path, config)

        self.verbose = self.config['args.verbose']
        
        self.root = self.config['path.root']
        self.content = self.config['path.content']
        self.game = self.config['path.game']

        self._setup_bindir()

    def _setup_bindir(self):
        self.bindir = os.path.join(self.root, self.game, 'bin', utilities.get_platform_bindir())
        if not os.path.exists(self.bindir):
            raise Exception('Could not find the bin directory')
        logging.debug(f'using bin directory {self.bindir}')

    def get_tool(self, tool: str):
        return os.path.join(self.bindir, tool)

    def run_tool(self, *args, **kwargs) -> int:
        predef = {}
        if not self.verbose:
            predef['stdout'] = subprocess.DEVNULL
        predef['stderr'] = subprocess.STDOUT

        result = subprocess.run(*args, **dict(predef, **kwargs))
        return result.returncode


class BuildContext():
    """
    A collection of assets with shared configuration
    """
    def __init__(self, config: dict):
        self.assets = []
        self.buildable = []
        self.config = config


class BuildSubsystem():
    """
    Represents a build system that implements custom behaviour.
    """
    def __init__(self, env: BuildEnvironment, config: dict):
        self.env = env
        self.config = config
    
    def build(self) -> bool:
        """
        Invokes the build logic of the subsystem.
        Returns True if success, otherwise False.
        """
        raise NotImplementedError()
    
    def clean(self) -> bool:
        """
        Removes all the output files generated by this subsystem.
        """
        raise NotImplementedError()


class PrecompileResult():
    def __init__(self, inputs: Set[Path], outputs: Set[Path]):
        self.inputs = inputs
        self.outputs = outputs


class Asset():
    """
    Represents an asset to be compiled
    """
    def __init__(self, path: Path, config: dict):
        self.id = uuid.uuid4()
        self.path = path
        self.config = config
    
    def get_id(self):
        """
        Returns the unique identifier for this asset
        """
        return self.id


class BaseDriver():
    """
    Represents an instance of a tool that compiles assets
    """
    def __init__(self, env: BuildEnvironment, config: dict):
        
        self.env = env
        self.config = config
        self.tool = self.env.get_tool(self._tool_name())

    def _tool_name(self):
        raise NotImplementedError()
    
    def precompile(self, context: BuildContext, asset: Asset) -> PrecompileResult:
        """
        Checks to ensure all required files are present
        Returns a list of source and output files to be hashed, or None if failure
        """
        raise NotImplementedError()


class SerialDriver(BaseDriver):
    def compile(self, context: BuildContext, asset: Asset) -> bool:
        """
        Performs the compile
        Returns True if success, otherwise False.
        """
        raise NotImplementedError()


class BatchedDriver(BaseDriver):
    def compile_all(self, context: BuildContext, assets: List[Asset]) -> bool:
        """
        Performs the compile
        Returns True if success, otherwise False.
        """
        raise NotImplementedError()
