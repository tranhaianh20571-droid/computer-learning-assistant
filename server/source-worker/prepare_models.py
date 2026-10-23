"""Deployment-only model download; never runs while handling user media."""
import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=("tiny", "base", "small", "medium", "large-v3"), default="small")
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    from faster_whisper.utils import download_model
    args.directory.mkdir(parents=True, exist_ok=True)
    download_model(args.model, output_dir=str(args.directory / args.model))
    print("ASR model prepared: " + args.model)


if __name__ == "__main__":
    main()
