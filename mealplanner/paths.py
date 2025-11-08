import pathlib

def package_root() -> pathlib.Path:
    # mealplanner/paths.py -> mealplanner/
    return pathlib.Path(__file__).resolve().parent

def data_dir() -> pathlib.Path:
    return package_root() / "data"

def default_outdir() -> pathlib.Path:
    return pathlib.Path.cwd() / "out"
