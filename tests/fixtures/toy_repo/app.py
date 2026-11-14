import argparse

from utils import shout


def greet(name: str) -> str:
    return f"Hello, {name}."


def main() -> None:
    parser = argparse.ArgumentParser(description="Toy greeter CLI.")
    parser.add_argument("name", help="Name to greet.")
    args = parser.parse_args()
    print(greet(args.name))


if __name__ == "__main__":
    main()
