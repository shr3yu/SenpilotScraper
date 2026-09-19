import zipfile
from pathlib import Path


def zip_directory(directory: Path, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in directory.iterdir():
            if file.is_file():
                zf.write(file, arcname=file.name)
    return output_path


if __name__ == "__main__":
    # Quick test - point at an existing downloads folder from the scraper.
    import sys
    test_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("downloads")
    sub = next(test_dir.iterdir(), None)
    if sub and sub.is_dir():
        out = zip_directory(sub, Path("outputs") / f"{sub.name}.zip")
        print(f"Zipped to {out} ({out.stat().st_size} bytes)")
    else:
        print("No download directory found to zip.")