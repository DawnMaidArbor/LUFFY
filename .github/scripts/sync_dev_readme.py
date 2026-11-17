#!/usr/bin/env python3
"""Reconcile README's Complete TODO List with markers in changed Python files."""
import codecs
import collections
import difflib
import json
import os
import pathlib
import re
import subprocess
import sys
import traceback
import urllib.request

TITLE_TEXT = "Complete TODO List"
TITLE_RE = re.compile(r"^\s*#{1,6}\s+.*" + re.escape(TITLE_TEXT) + r"\s*$", re.IGNORECASE)
HEADING_END_RE = re.compile(r"^\s*(#{1,3})(?:\s+|$)")
BULLET_RE = re.compile(
    r"^(?P<head>\s*-\s*\[(?: |x|X)\]\s+\*\*)"
    r"(?P<path>.*?):(?P<line>\d+)"
    r"(?P<sep>\*\*\s*-\s+)(?P<desc>.*?)(?P<tail>[ \t]*$)"
)
TODO_RE = re.compile(r"#\s*TODO\b(.*)$", re.IGNORECASE)


def path_key(path):
    p = (path or "").replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p


def norm_text(text):
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def git(*args):
    return subprocess.check_output(["git", *args], text=True, stderr=subprocess.STDOUT).strip()


def toggle_fence(line, in_fence):
    if re.match(r"^\s*(`{3,}|~{3,})", line):
        return not in_fence, True
    return in_fence, False


def parse_readme(text):
    lines = text.splitlines()
    in_fence = False
    title_indices = []
    for i, line in enumerate(lines):
        in_fence, is_fence = toggle_fence(line, in_fence)
        if is_fence or in_fence:
            continue
        hm = re.match(r"^\s*(#{1,6})\s+", line)
        if hm and TITLE_RE.match(line) and len(hm.group(1)) == 3:
            title_indices.append(i)
    if len(title_indices) != 1:
        raise RuntimeError(f"expected exactly one level-3 TODO title, found {len(title_indices)}")
    title_idx = title_indices[0]
    title_line = lines[title_idx]

    end_idx = len(lines)
    in_fence = False
    for i in range(title_idx + 1, len(lines)):
        in_fence, _ = toggle_fence(lines[i], in_fence)
        if not in_fence and HEADING_END_RE.match(lines[i]):
            end_idx = i
            break

    bullets = []
    in_fence = False
    for i in range(title_idx + 1, end_idx):
        in_fence, _ = toggle_fence(lines[i], in_fence)
        if in_fence:
            continue
        m = BULLET_RE.match(lines[i])
        if not m:
            continue
        item = m.groupdict()
        item.update({
            "idx": i,
            "raw": lines[i],
            "key_path": path_key(item["path"]),
            "key_desc": norm_text(item["desc"]),
            "line_no": int(item["line"]),
        })
        bullets.append(item)
    return {"lines": lines, "title_idx": title_idx, "title_line": title_line,
            "end_idx": end_idx, "bullets": bullets}


def parse_python_todos(rel_path):
    path = pathlib.Path(rel_path)
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    result = []
    for lineno, raw_line in enumerate(text.splitlines(), 1):
        m = TODO_RE.search(raw_line)
        if not m:
            continue
        after = m.group(1).strip()
        display = re.sub(r"^\s*:\s*", "", after).strip()
        display = re.sub(r"\s+", " ", display)
        result.append({"line": lineno, "raw": raw_line, "after": after,
                       "display": display, "key_desc": norm_text(after)})
    return result


def match_old_to_new(old_items, new_items):
    a = [x["key_desc"] for x in old_items]
    b = [x["key_desc"] for x in new_items]
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    pairs = {}
    used_old = set()
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for oi, ni in zip(range(i1, i2), range(j1, j2)):
                pairs[ni] = oi
                used_old.add(oi)
        elif tag == "replace":
            candidates = []
            for oi in range(i1, i2):
                for ni in range(j1, j2):
                    ratio = difflib.SequenceMatcher(None, a[oi], b[ni]).ratio()
                    if ratio >= 0.70:
                        candidates.append((-ratio, oi, ni))
            selected = []
            used_new_block = set()
            for _, oi, ni in sorted(candidates):
                if oi in used_old or ni in used_new_block:
                    continue
                if any((oi - old0) * (ni - new0) < 0 for old0, new0 in selected):
                    continue
                selected.append((oi, ni))
                used_new_block.add(ni)
                used_old.add(oi)
                pairs[ni] = oi
    return pairs


def render_old_number(old_item, new_line):
    m = BULLET_RE.match(old_item["raw"])
    if not m:
        raise RuntimeError("old README bullet no longer matches parser")
    g = m.groupdict()
    return g["head"] + g["path"] + ":" + str(new_line) + g["sep"] + g["desc"] + g["tail"]


def render_new_bullet(path, todo, template):
    if template is not None:
        m = BULLET_RE.match(template["raw"])
        head, sep, display_path = m.group("head"), m.group("sep"), m.group("path")
    else:
        head, sep, display_path = "- [ ] **", "** - ", path
    display = todo["display"] or "TODO"
    return f"{head}{display_path}:{todo['line']}{sep}{display}".rstrip()


def notify_failure(details):
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        return
    repo = os.environ.get("GITHUB_REPOSITORY", "DawnMaidArbor/LUFFY")
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    body = ("Automated README TODO synchronization failed before pushing.\n\n"
            f"Run: https://github.com/{repo}/actions/runs/{run_id}\n\n"
            "```text\n" + details[-45000:] + "\n```")
    payload = json.dumps({"title": "CI failure: sync dev TODO list into README", "body": body}).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues", data=payload, method="POST",
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/vnd.github+json", "User-Agent": "luffy-todo-sync-ci"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


def main():
    cwd = pathlib.Path.cwd()
    if not (cwd / ".git").exists() or not (cwd / "README.md").exists():
        print("syntax/local check OK; target checkout not present, skipping reconciliation")
        return

    readme_path = cwd / "README.md"
    raw_bytes = readme_path.read_bytes()
    has_bom = raw_bytes.startswith(codecs.BOM_UTF8)
    text = raw_bytes.decode("utf-8-sig")
    newline = "\r\n" if b"\r\n" in raw_bytes else "\n"
    ends_with_newline = text.endswith("\n")

    parsed = parse_readme(text)
    lines = parsed["lines"]
    bullets = parsed["bullets"]
    groups = collections.OrderedDict()
    lower_group = {}
    for item in bullets:
        groups.setdefault(item["key_path"], []).append(item)
        lower_group[item["key_path"].lower()] = item["key_path"]

    base_ref = os.environ.get("DIFF_BASE", "origin/main")
    head_ref = os.environ.get("DIFF_HEAD", "HEAD")
    changed_py = sorted({path_key(x.strip()) for x in
                          git("diff", "--name-only", base_ref, head_ref).splitlines()
                          if x.strip().endswith(".py")})
    all_py = [x.strip() for x in git("ls-files").splitlines() if x.strip().endswith(".py")]
    source_cache = {}

    def source_for(rel):
        if rel not in source_cache:
            if rel in all_py or pathlib.Path(rel).is_file():
                source_cache[rel] = parse_python_todos(rel)
            else:
                source_cache[rel] = []
        return source_cache[rel]

    for py_file in all_py:
        source_for(py_file)

    edits = []
    change_report = {}
    changed_lower = {x.lower() for x in changed_py}

    for rel in changed_py:
        lookup_key = lower_group.get(rel.lower(), rel)
        old_items = groups.get(lookup_key, [])
        todos = source_for(rel)
        pairs = match_old_to_new(old_items, todos) if old_items else {}
        rendered = []
        for new_idx, todo in enumerate(todos):
            old_idx = pairs.get(new_idx)
            if old_idx is None:
                rendered.append(render_new_bullet(rel, todo, old_items[0] if old_items else None))
            else:
                rendered.append(render_old_number(old_items[old_idx], todo["line"]))

        if old_items:
            first, last = old_items[0], old_items[-1]
            for physical_idx in range(first["idx"], last["idx"] + 1):
                physical = lines[physical_idx]
                if not physical.strip():
                    continue
                check = BULLET_RE.match(physical)
                if not check:
                    raise RuntimeError(f"non-bullet content inside group {rel} line {physical_idx + 1}: {physical[:160]}")
                if path_key(check.group("path")) != lookup_key:
                    raise RuntimeError(f"interleaved README path in group {rel} line {physical_idx + 1}")
            start, end = first["idx"], last["idx"] + 1
        else:
            later_paths = sorted(k for k in groups.keys() if k > lookup_key)
            anchor = groups[later_paths[0]][0]["idx"] if later_paths else parsed["end_idx"]
            start = end = anchor

        rendered_lines = [int(BULLET_RE.match(x).group("line")) for x in rendered]
        if rendered_lines != [t["line"] for t in todos]:
            raise RuntimeError(f"line order mismatch for {rel}: {rendered_lines} vs {[t['line'] for t in todos]}")
        for bullet_text in rendered:
            if not BULLET_RE.match(bullet_text):
                raise RuntimeError(f"invalid generated bullet for {rel}: {bullet_text[:200]}")

        edits.append((start, end, rendered, lookup_key))
        change_report[rel] = {"old": len(old_items), "markers": len(todos), "new": len(rendered),
                              "removed": len(old_items) - len(rendered),
                              "inserted": len(todos) - len(pairs), "lines": rendered_lines}

    updated_lines = list(lines)
    for start, end, replacement, _ in sorted(edits, key=lambda x: x[0], reverse=True):
        updated_lines[start:end] = replacement
    output_text = newline.join(updated_lines)
    if ends_with_newline:
        output_text += newline

    reparsed = parse_readme(output_text)
    final_bullets = reparsed["bullets"]
    sort_keys = [(b["key_path"], b["line_no"]) for b in final_bullets]
    if sort_keys != sorted(sort_keys):
        raise RuntimeError("final TODO bullets are not sorted by path then numeric line")
    if reparsed["title_line"] != parsed["title_line"] or reparsed["title_idx"] != parsed["title_idx"]:
        raise RuntimeError("TODO section title/position changed")
    if output_text.count(TITLE_TEXT) != text.count(TITLE_TEXT):
        raise RuntimeError("TODO title occurrence count changed")

    final_groups = collections.OrderedDict()
    for item in final_bullets:
        final_groups.setdefault(item["key_path"], []).append(item)
    expected_total = len(bullets) + sum(len(rep) - (end - start) for start, end, rep, _ in edits)
    if len(final_bullets) != expected_total:
        raise RuntimeError(f"bullet count {len(final_bullets)} != expected {expected_total}")

    for path, old_group in groups.items():
        if path.lower() in changed_lower:
            continue
        if [b["raw"] for b in final_groups.get(path, [])] != [b["raw"] for b in old_group]:
            raise RuntimeError(f"unchanged file group was modified: {path}")

    for rel in changed_py:
        key = lower_group.get(rel.lower(), rel)
        want_lines = [t["line"] for t in source_for(rel)]
        got_items = final_groups.get(key, [])
        if [b["line_no"] for b in got_items] != want_lines:
            raise RuntimeError(f"source/README marker mismatch for {rel}")
        if [b["key_path"] for b in got_items] != [key] * len(got_items):
            raise RuntimeError(f"path key mismatch after reconciliation for {rel}")

    output_bytes = (codecs.BOM_UTF8 if has_bom else b"") + output_text.encode("utf-8")
    if output_bytes != raw_bytes:
        readme_path.write_bytes(output_bytes)
    print("TODO_SYNC_REPORT=" + json.dumps({"tracked_python_files": len(all_py),
        "changed_python_files": changed_py, "original_bullets": len(bullets),
        "final_bullets": len(final_bullets), "edits": change_report}, ensure_ascii=False, sort_keys=True))
    print("".join(difflib.unified_diff(text.splitlines(keepends=True),
                                      output_text.splitlines(keepends=True),
                                      fromfile="a/README.md", tofile="b/README.md", n=2))[:30000])


if __name__ == "__main__":
    try:
        main()
    except Exception:
        details = traceback.format_exc()
        print(details, file=sys.stderr)
        try:
            notify_failure(details)
        except Exception as notify_exc:
            print(f"notification failed: {notify_exc}", file=sys.stderr)
        sys.exit(1)
