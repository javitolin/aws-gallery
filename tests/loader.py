import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LAMBDAS = os.path.join(HERE, "..", "lambdas")


def load(name, *path_parts):
    """Load a Lambda module under a unique name.

    Every Lambda has its own handler.py, so a plain import would let whichever
    test ran first win via sys.modules.
    """
    path = os.path.join(LAMBDAS, *path_parts)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
