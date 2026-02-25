"""Parchiver CLI for document archiving."""

from dotenv import load_dotenv

load_dotenv()

import typer

app = typer.Typer(name="parchiver", help="Document archiver for investment materials")


def main():
    app()


if __name__ == "__main__":
    main()
