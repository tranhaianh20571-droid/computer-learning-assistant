"""Read synthetic benchmark HTML from stdin; never persist page contents."""
import json
import sys
from html.parser import HTMLParser


class Reader(HTMLParser):
    hidden_tags = {"script", "style", "noscript", "svg", "nav", "footer"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.hidden_tags:
            self.hidden += 1
        if tag in {"p", "div", "br", "h1", "h2", "h3", "li", "pre", "section"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.hidden_tags and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def main():
    result = []
    for value in json.load(sys.stdin):
        reader = Reader()
        reader.feed(value)
        result.append("\n".join(
            " ".join(line.split())
            for line in "".join(reader.parts).splitlines()
            if line.strip()
        ))
    json.dump(result, sys.stdout, ensure_ascii=True)


if __name__ == "__main__":
    main()
