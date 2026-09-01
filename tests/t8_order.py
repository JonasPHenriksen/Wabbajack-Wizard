"""Download order: smallest first, largest first, or as the manifest lists them."""
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOXROOT = os.path.join(os.environ.get("TEMP", "/tmp"), "wj_tests")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Sandbox, check, summary

sys.path.insert(0, REPO)
os.chdir(REPO)
import logging
import wabbajack_archive_downloader as D
D.LOG.setLevel(logging.CRITICAL)

BOX = os.path.join(BOXROOT, "sb8")
G = "Fallout4"

print("=" * 70)
print("  T8  DOWNLOAD ORDER")
print("=" * 70)

# ---------------------------------------------------------------- 8.1
print("\n 8.1  the sort key")
check("smaller sorts before larger",
      D.size_key({"size": 10}) < D.size_key({"size": 20}))
check("unknown size sorts after every known size",
      D.size_key({"size": None}) > D.size_key({"size": 10 ** 12}))
check("largest_first reverses known sizes",
      D.size_key({"size": 20}, True) < D.size_key({"size": 10}, True))
check("unknown size still sorts last when largest first",
      D.size_key({"size": None}, True) > D.size_key({"size": 1}, True))
check("a missing size key behaves like None",
      D.size_key({}) == D.size_key({"size": None}))

# ---------------------------------------------------------------- 8.2
print("\n 8.2  order_downloads sorts both work lists")
mk = lambda n, s: {"name": n, "size": s}
direct = [("http", mk("d-big.7z", 900)), ("http", mk("d-small.7z", 100))]
nexus = [mk("n-mid.7z", 500), mk("n-none.7z", None), mk("n-small.7z", 50)]
label = D.order_downloads(direct, nexus, "smallest")
check("returns a label", label == "smallest first", str(label))
check("direct sorted ascending",
      [a["name"] for _k, a in direct] == ["d-small.7z", "d-big.7z"],
      str([a["name"] for _k, a in direct]))
check("nexus sorted ascending with unknown last",
      [a["name"] for a in nexus] == ["n-small.7z", "n-mid.7z", "n-none.7z"],
      str([a["name"] for a in nexus]))

label = D.order_downloads(direct, nexus, "largest")
check("largest first reverses", [a["name"] for a in nexus]
      == ["n-mid.7z", "n-small.7z", "n-none.7z"],
      str([a["name"] for a in nexus]))
check("  and its label says so", label == "largest first", str(label))

before = [a["name"] for a in nexus]
check("'manifest' leaves the order untouched",
      D.order_downloads(direct, nexus, "manifest") is None
      and [a["name"] for a in nexus] == before)

# ---------------------------------------------------------------- 8.3
print("\n 8.3  end to end: the dry-run lists files in the chosen order")
s = Sandbox(BOX)
s.manifest("ListA", G, [("huge.7z", 5000, "nexus"),
                        ("tiny.7z", 10, "nexus"),
                        ("medium.7z", 800, "nexus"),
                        ("small.7z", 90, "http")])


def listed(out):
    """Archive names in the order the dry-run prints them."""
    if "FILES THAT WOULD BE DOWNLOADED" not in out:
        return []
    body = out.split("FILES THAT WOULD BE DOWNLOADED", 1)[1]
    body = body.split("[DRY-RUN COMPLETE]", 1)[0]
    return re.findall(r"\[(?:nexus|http|wabbajackcdn|mediafire)\]\s+(\S+)", body)


rc, out = s.run("wabbajack_archive_downloader.py",
                ["--game", G, "--list", "ListA", "--dry-run", "--yes",
                 "--order", "smallest"], expect=0)
names = listed(out)
check("run says which order it is using", "smallest first" in out)
# Direct sources are fetched before Nexus ones, so compare within each group.
nex = [n for n in names if n in ("huge.7z", "tiny.7z", "medium.7z")]
check("Nexus files ascending by size",
      nex == ["tiny.7z", "medium.7z", "huge.7z"], str(names))

rc, out = s.run("wabbajack_archive_downloader.py",
                ["--game", G, "--list", "ListA", "--dry-run", "--yes",
                 "--order", "largest"], expect=0)
names = listed(out)
nex = [n for n in names if n in ("huge.7z", "tiny.7z", "medium.7z")]
check("largest first is honoured",
      nex == ["huge.7z", "medium.7z", "tiny.7z"], str(names))
check("  and reported", "largest first" in out)

rc, out = s.run("wabbajack_archive_downloader.py",
                ["--game", G, "--list", "ListA", "--dry-run", "--yes",
                 "--order", "manifest"], expect=0)
names = listed(out)
nex = [n for n in names if n in ("huge.7z", "tiny.7z", "medium.7z")]
check("manifest order is the order it is declared in",
      nex == ["huge.7z", "tiny.7z", "medium.7z"], str(names))

# ---------------------------------------------------------------- 8.4
print("\n 8.4  the updater passes the order down to the downloader")
import wj_autoupdate as A


class FakeArgs:
    allow_size_only = False
    order = "largest"


check("--order reaches the child command",
      "--order" in A.child_flags(FakeArgs()) and
      "largest" in A.child_flags(FakeArgs()), str(A.child_flags(FakeArgs())))

FakeArgs.order = None
saved = A.DOWNLOAD_ORDER
A.DOWNLOAD_ORDER = "smallest"
try:
    check("the updater's own setting is used when no flag is given",
          A.child_flags(FakeArgs()) == ["--order", "smallest"],
          str(A.child_flags(FakeArgs())))
    A.DOWNLOAD_ORDER = None
    check("and nothing is passed when neither is set",
          A.child_flags(FakeArgs()) == [], str(A.child_flags(FakeArgs())))
finally:
    A.DOWNLOAD_ORDER = saved

sys.exit(summary())
