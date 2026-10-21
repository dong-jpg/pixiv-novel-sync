import ast, os, re, subprocess, sys

ROOT = r"D:\gitcode\pixiv-novel-sync"
SRC = os.path.join(ROOT, "src", "pixiv_novel_sync")

paths = [os.path.join(SRC, f) for f in ["webapp.py", "ai_web.py", "preference_web.py", "rescue_web.py"]]

routes = []

class V(ast.NodeVisitor):
    def __init__(self, fname):
        self.fname = fname
    def visit_Call(self, node):
        fname = None
        path = None
        lineno = node.lineno
        if isinstance(node.func, ast.Attribute) and node.func.attr in ("route", "add_url_rule"):
            # first arg could be path
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                path = node.args[0].value
            for kw in node.keywords:
                if kw.arg in ("rule", "path") and isinstance(kw.value, ast.Constant):
                    path = kw.value.value
        if path and isinstance(path, str):
            routes.append((os.path.basename(self.fname), lineno, node.func.attr, path))
        self.generic_visit(node)

for p in paths:
    tree = ast.parse(open(p, encoding="utf-8").read())
    V(p).visit(tree)

# normalize to grep-able core: strip flask converters like <int:...> -> take literal prefix chunks
def fuzz(path):
    # split at '<' and extract meaningful literal fragments >= 8 chars
    frags = re.split(r"<[^>]*>", path)
    frags = [f.rstrip("/-") for f in frags]
    frags = [f for f in frags if len(f) >= 6]
    return frags

# search roots: templates, userscripts, static, root js, docs are ignored; also grep src for url_for / hardcoded usage
search_dirs = {
    "templates": os.path.join(SRC, "templates"),
    "userscripts": os.path.join(ROOT, "userscripts"),
    "cli": os.path.join(SRC, "cli.py"),
}

results = []
for fname, lineno, kind, path in sorted(set(routes)):
    frags = fuzz(path)
    hits = {"templates": 0, "userscripts": 0, "cli": 0}
    for f in frags:
        for name, d in search_dirs.items():
            if os.path.isdir(d):
                r = subprocess.run(["grep", "-rF", "-l", f, d], capture_output=True, text=True)
                hits[name] += len([l for l in r.stdout.splitlines() if l.strip()])
            else:
                r = subprocess.run(["grep", "-F", "-c", f, d], capture_output=True, text=True)
                try:
                    hits[name] += int(r.stdout.strip() or 0)
                except ValueError:
                    pass
    results.append((fname, lineno, path, hits, frags))

with open(r"D:\gitcode\pixiv-novel-sync\audit_routes_out.txt", "w", encoding="utf-8") as out:
    for fname, lineno, path, hits, frags in results:
        main_hit = hits["templates"] + hits["userscripts"] + hits["cli"]
        out.write(f"{fname}:{lineno} {path} callers={hits} frags={frags}\n")
print("done", len(results))
