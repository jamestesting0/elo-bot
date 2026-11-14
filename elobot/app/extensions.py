import os


def get_extensions(ext_folder: str) -> list[str]:
    extensions = [
        f"{ext_folder}.{f[:-3]}"
        for f in os.listdir(ext_folder)
        if f.endswith("_commands.py") and not f.startswith("_")
    ]

    return extensions
