"""wiki/ 무결성 검사: frontmatter/type 누락, 깨진 링크, 고아 페이지.

사용법: python tools/wiki_lint.py  (vault 루트 어디서 실행해도 됨)
종료 코드: 문제가 하나라도 있으면 1, 없으면 0.
"""
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

VAULT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIKI = os.path.join(VAULT, "wiki")
RESERVED = {"index.md", "log.md"}
LINK = re.compile(r"\]\(([^)]+)\)")
RESOURCE = re.compile(r"^resource: (.+)$", re.M)

def wiki_pages():
    for dirpath, _, files in os.walk(WIKI):
        for name in files:
            if name.endswith(".md"):
                yield os.path.join(dirpath, name)

def main():
    problems = 0
    inbound = set()  # 링크로 한 번이라도 가리켜진 위키 페이지들

    for path in wiki_pages():
        rel = os.path.relpath(path, WIKI)
        text = open(path, encoding="utf-8").read()

        # 1) frontmatter/type (예약 파일 제외; 루트 index.md의 okf_version은 허용)
        if os.path.basename(path) not in RESERVED:
            if not text.startswith("---\n"):
                print(f"[frontmatter 없음] {rel}")
                problems += 1
            elif not re.search(r"^type: .+$", text.split("\n---", 1)[0], re.M):
                print(f"[type 없음] {rel}")
                problems += 1

        # 2) 링크·resource 대상 존재 여부 (코드 블록·인라인 코드 안은 예시로 보고 제외)
        prose = re.sub(r"```.*?```", "", text, flags=re.S)
        prose = re.sub(r"`[^`\n]*`", "", prose)
        targets = LINK.findall(prose) + RESOURCE.findall(text)
        for link in targets:
            if link.startswith(("http://", "https://", "#", "mailto:")):
                continue
            if link.startswith("/"):
                print(f"[절대 경로 링크] {rel} -> {link} (상대 경로로 바꿀 것)")
                problems += 1
                continue
            target = os.path.normpath(os.path.join(os.path.dirname(path), link.split("#")[0]))
            if not os.path.exists(target):
                print(f"[깨진 링크] {rel} -> {link}")
                problems += 1
            elif target.startswith(WIKI):
                inbound.add(target)

    # 3) 고아 페이지 (인바운드 링크 0개; index/log/CLAUDE.md 제외)
    for path in wiki_pages():
        name = os.path.basename(path)
        if name in RESERVED or name == "CLAUDE.md":
            continue
        if path not in inbound:
            print(f"[고아 페이지] {os.path.relpath(path, WIKI)} (들어오는 링크 없음)")
            problems += 1

    print(f"\n{'문제 ' + str(problems) + '건' if problems else '통과: 문제 없음'}")
    return 1 if problems else 0

if __name__ == "__main__":
    sys.exit(main())
