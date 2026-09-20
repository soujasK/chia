import re
import sys

def count_pages(pdf_path):
    with open(pdf_path, 'rb') as f:
        data = f.read()
    pages = len(re.findall(rb'/Type\s*/Page\b', data))
    print(f"{pdf_path}: {pages} pages ({len(data)} bytes)")
    return pages

if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "paper.pdf"
    count_pages(path)
