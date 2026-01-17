#!/usr/bin/env python3
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

# Stable linux kernel source
#LINUX_REMOTE = "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git"
#LINUX_BRANCH = "linux-6.19.y"
#LINUX_REF = "v6.19-rc5"  # tag/branch/commit to pin
LINUX_REMOTE="https://gitlab.freedesktop.org/agd5f/linux.git"
LINUX_BRANCH="amd-drm-fixes-6.19-2026-01-15"
LINUX_REF=""

# PKGBUILD knobs
PKGVER = "6.19.0rc5"
#KERNEL_SOURCE = "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.18.5.tar.xz"
KERNEL_SOURCE = "https://gitlab.freedesktop.org/agd5f/linux/-/archive/amd-drm-fixes-6.19-2026-01-15/linux-amd-drm-fixes-6.19-2026-01-15.tar.gz"

# Ubuntu linux kernel source - base used by tuxedo branch
UBUNTU_CODENAME = "noble"
UBUNTU_BRANCH = "hwe-6.17-next"
UBUNTU_REMOTE = f"git://git.launchpad.net/~ubuntu-kernel/ubuntu/+source/linux/+git/{UBUNTU_CODENAME}"
UBUNTU_REF = ""  # leave empty to auto-resolve merge-base with tuxedo

# Tuxedo linux kernel source
TUXEDO_REMOTE = "https://gitlab.com/tuxedocomputers/development/packages/linux.git"
TUXEDO_BRANCH = "tuxedo-6.17-24.04"
TUXEDO_REF = ""  # tag/branch/commit to pin

# Optional: manually exclude specific Tuxedo commit hashes (full or abbreviated) from the patchset.
# Example: EXCLUDE_COMMITS = {"deadbeef", "abc123"}
EXCLUDE_COMMITS: set[str] = {
    "27b53f08bb8b7dcf4e9ae551bc8f9c65a05568ca",  # TUXEDO: Add automatic update scripts
    "1fe04fc050d845eedd28cc16e768e4eac66891a8",  # TUXEDO: Initialize abstracted debian directory
}

# CachyOS PKGBUILD source
CACHYOS_PKGBUILDS_REMOTE = "https://github.com/CachyOS/linux-cachyos"
CACHYOS_PKGBUILDS_BRANCH = "master"
CACHYOS_PKGBUILDS_REF = "8e4d77a4aeef28c8e93fd9b724d61a84b11b384f"  # tag/branch/commit to pin (pkgver 6.18.5)

# CachyOS PATCHES source
CACHYOS_PATCHES_REMOTE = "https://github.com/CachyOS/kernel-patches.git"
CACHYOS_PATCHES_BRANCH = "master"
CACHYOS_PATCHES_REF = "af948449e6e97afbac82dc9887e2b2b95d1a6519"  # tag/branch/commit to pin
CACHYOS_PATCHES_FOLDER = "6.19"

# CachyOS package selection
CACHYOS_PKG_DIR = "linux-cachyos-rc" 

ROOT = Path(__file__).resolve().parent.parent
LINUX_REPO = ROOT / "linux"
LINUX_CACHYOS_REPO = ROOT / "cachyos-pkgbuilds"
LINUX_CACHYOS_PATCHES_REPO = ROOT / "cachyos-patches"
PACKAGE_PATCHES_DIR = ROOT / "package" / "patches"
PACKAGE_CONFIG_PATH = ROOT / "package" / "config"
Patch = tuple[str, str]
PKGBUILD_TEMPLATE = ROOT / "scripts" / "PKGBUILD"
PACKAGE_PKGFILE = ROOT / "package" / "PKGBUILD"

def run_git(args, capture=False, check=True, stdin_data: str | None = None, env=None):
    """Run a git (or git-related) command and optionally capture output."""
    print("$", " ".join(map(str, args)), flush=True)
    res = subprocess.run(args, capture_output=capture, text=True, input=stdin_data, env=env)
    if check and res.returncode != 0:
        stderr = (res.stderr or "").strip()
        stdout = (res.stdout or "").strip()
        msg = stderr or stdout or "git command failed"
        raise subprocess.CalledProcessError(res.returncode, args, output=res.stdout, stderr=msg)
    return res

def list_refs(repo: str, pattern: str, sort: str | None = None):
    """List refs in a repository matching a pattern (e.g., refs/remotes/*)."""
    cmd = ["git", "-C", repo, "for-each-ref", "--format=%(refname)"]
    if sort:
        cmd.append(f"--sort={sort}")
    cmd.append(pattern)
    res = run_git(cmd, capture=True, check=False)
    return [r.strip() for r in res.stdout.splitlines() if r.strip()]

def rev_list(repo: str, ref: str):
    """Return the set of non-merge commit hashes reachable from a ref."""
    res = run_git(["git", "-C", repo, "rev-list", "--no-merges", ref], capture=True, check=False)
    if res.returncode != 0:
        return set()
    return {line.strip() for line in res.stdout.splitlines() if line.strip()}

def bulk_subjects(repo: str, hashes):
    """Lookup commit subjects for many hashes efficiently via xargs/git show."""
    if not hashes:
        return {}
    with tempfile.NamedTemporaryFile(mode="w+", delete=False) as tmp:
        for h in hashes:
            tmp.write(h + "\n")
        tmp_path = tmp.name
    try:
        cmd = ["xargs", "-a", tmp_path, "-r", "git", "-C", repo, "show", "-s", "--format=%H %s"]
        print("$", " ".join(map(str, cmd)), flush=True)
        res = run_git(cmd, capture=True, check=False)
        mapping = {}
        for line in res.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split(" ", 1)
            if len(parts) == 2:
                mapping[parts[0]] = parts[1]
            else:
                mapping[parts[0]] = ""
        return mapping
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def rev_list_range(repo: str, include: str, exclude: list[str], reverse: bool = False) -> list[str]:
    """Return commits reachable from include but not from the exclude refs."""
    args = ["git", "-C", repo, "rev-list", "--no-merges"]
    if reverse:
        args.append("--reverse")
    args.append(include)
    for ex in exclude:
        if ex:
            args.append(f"^{ex}")
    res = run_git(args, capture=True, check=False)
    if res.returncode != 0:
        sys.exit(res.stderr or res.stdout or f"rev-list failed for {include}")
    return [line.strip() for line in res.stdout.splitlines() if line.strip()]


def order_on_ref(repo: str, ref: str, include_set: set):
    """Preserve commit order from a specific ref while filtering to a hash set."""
    res = run_git(["git", "-C", repo, "rev-list", "--reverse", "--no-merges", ref], capture=True, check=False)
    ordered = []
    for h in res.stdout.splitlines():
        if h in include_set:
            ordered.append(h)
    return ordered

def ensure_remote(repo_dir, remote_name: str, remote_url: str, branch: str, fetch_all: bool = False):
    """Ensure repository exists and remote is present/set; optionally fetch all refs."""
    repo_path = Path(repo_dir)

    if not repo_path.exists():
        run_git(["git", "clone", "--origin", remote_name, "--no-checkout", remote_url, str(repo_path)])

    if not (repo_path / ".git").exists():
        raise ValueError(f"{repo_path} exists but is not a git repository")

    remotes_res = run_git(["git", "-C", str(repo_path), "remote"], capture=True, check=False)
    remotes = {r.strip() for r in remotes_res.stdout.splitlines() if r.strip()}
    if remote_name not in remotes:
        run_git(["git", "-C", str(repo_path), "remote", "add", remote_name, remote_url])
    else:
        run_git(["git", "-C", str(repo_path), "remote", "set-url", remote_name, remote_url])

    if fetch_all:
        run_git(["git", "-C", str(repo_path), "fetch", remote_name, "--tags"], check=False)
        run_git(["git", "-C", str(repo_path), "fetch", remote_name, "+refs/heads/*:refs/remotes/" + remote_name + "/*"], check=False)


def ensure_ref(repo_dir: Path, remote_name: str, remote_url: str, branch: str, ref: str, label: str) -> str:
    """Fetch a ref (or branch tip when ref is empty) into a pinned namespace and return the ref path."""

    target_ref = ref or branch
    if not target_ref:
        sys.exit(f"missing required ref for {label}")

    ensure_remote(repo_dir, remote_name, remote_url, branch)

    if target_ref.startswith(f"refs/remotes/{remote_name}/") and list_refs(str(repo_dir), target_ref):
        return target_ref

    pin_ref = f"refs/remotes/{remote_name}/pin-{label}"
    # Force-update the pinned ref so we don't fail on non-fast-forward (e.g., moving from newer to older tags).
    fetch_res = run_git(["git", "-C", str(repo_dir), "fetch", remote_name, f"+{target_ref}:{pin_ref}"], capture=True, check=False)
    if fetch_res.returncode != 0:
        sys.exit(f"failed to fetch ref {target_ref} for {label} from {remote_name}: {fetch_res.stderr}")
    return pin_ref

def checkout(repo_dir, remote_name: str, remote_url: str, branch: str, ref: str = ""):
    """Ensure repo_dir exists, remote is set, and switch to a specific ref (branch tip when ref is empty)."""

    repo_path = Path(repo_dir)
    ensure_remote(repo_path, remote_name, remote_url, branch)

    target_ref = ref or branch
    if not target_ref:
        sys.exit(f"missing required ref for checkout in {repo_path}")

    fetch_res = run_git(["git", "-C", str(repo_path), "fetch", remote_name, target_ref], capture=True, check=False)
    if fetch_res.returncode != 0:
        sys.exit(f"Failed to fetch ref {target_ref} from {remote_name} in {repo_path}: {fetch_res.stderr}")
    run_git(["git", "-C", str(repo_path), "switch", "--detach", "FETCH_HEAD"])


def resolve_ubuntu_base(tuxedo_ref: str) -> str:
    """Resolve Ubuntu base ref: explicit UBUNTU_REF if set, otherwise merge-base with branch tip."""

    if UBUNTU_REF:
        return ensure_ref(LINUX_REPO, "ubuntu", UBUNTU_REMOTE, UBUNTU_BRANCH, UBUNTU_REF, "ubuntu")

    # If auto, prefer the newest Ubuntu hwe tag already merged into tuxedo to stay close to vendor base.
    ensure_remote(LINUX_REPO, "ubuntu", UBUNTU_REMOTE, UBUNTU_BRANCH)
    run_git(["git", "-C", str(LINUX_REPO), "fetch", "ubuntu", "--tags"], check=False)
    tag_pattern = "Ubuntu-hwe-6.17-*"
    tags_res = run_git(
        [
            "git",
            "-C",
            str(LINUX_REPO),
            "tag",
            "--list",
            tag_pattern,
            "--merged",
            tuxedo_ref,
            "--sort=-version:refname",
        ],
        capture=True,
        check=False,
    )
    for line in tags_res.stdout.splitlines():
        tag = line.strip()
        if not tag:
            continue
        tag_hash = run_git(["git", "-C", str(LINUX_REPO), "rev-parse", tag], capture=True, check=False)
        if tag_hash.returncode == 0 and tag_hash.stdout.strip():
            return tag_hash.stdout.strip().splitlines()[0]
        break

    # Fetch branch tip and pin it, then pick merge-base with tuxedo.
    ubuntu_tip_ref = ensure_ref(LINUX_REPO, "ubuntu", UBUNTU_REMOTE, UBUNTU_BRANCH, "", "ubuntu-tip")
    mb_res = run_git([
        "git",
        "-C",
        str(LINUX_REPO),
        "merge-base",
        tuxedo_ref,
        ubuntu_tip_ref,
    ], capture=True, check=False)
    if mb_res.returncode != 0 or not mb_res.stdout.strip():
        sys.exit("failed to determine ubuntu merge-base with tuxedo")
    return mb_res.stdout.strip().splitlines()[0]


def collect_cachyos_patches(patches_repo: Path, folder: str, pkgbuild_path: Path, *, lto_mode: str = "thin", cpusched: str = "bore") -> list[Patch]:
    """Mirror the CachyOS PKGBUILD patch loop: resolve the source array and load every .patch entry."""

    env = os.environ.copy()
    env.update({
        "PKGBUILD_PATH": str(pkgbuild_path),
        "_use_llvm_lto": lto_mode,
        "_cpusched": cpusched,
        "_build_nvidia_open": "no",
        "_build_zfs": "no",
    })

    cmd = [
        "bash",
        "-lc",
        'source "${PKGBUILD_PATH}" >/dev/null; printf "%s\n" "${source[@]}"'
    ]
    print("$", " ".join(cmd), flush=True)
    res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if res.returncode != 0:
        sys.exit(res.stderr or res.stdout or "failed to parse CachyOS PKGBUILD sources")

    patch_urls = [line.strip() for line in res.stdout.splitlines() if line.strip().endswith(".patch")]
    if not patch_urls:
        raise RuntimeError("No CachyOS patches discovered from PKGBUILD")

    collected: list[Patch] = []
    marker = f"/{folder}/"
    for idx, url in enumerate(patch_urls, start=1):
        rel = None
        if marker in url:
            rel = url.split(marker, 1)[1]
        else:
            rel = url.rsplit("/", 1)[-1]
        src_path = patches_repo / folder / rel
        if not src_path.exists():
            raise FileNotFoundError(f"Missing CachyOS patch referenced by PKGBUILD: {src_path}")
        content = src_path.read_text()
        label = re.sub(r"^[0-9]+-", "", Path(rel).stem)
        collected.append((label, content))

    return collected


def stage_cachyos_config(config_src: Path, dest_path: Path):
    """Copy CachyOS config into package directory, replacing any existing file."""
    if not config_src.exists():
        raise FileNotFoundError(f"Missing CachyOS config: {config_src}")
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(config_src, dest_path)


def create_patches_tarball(patches_dir: Path, archive_path: Path):
    """Tar.gz the patches directory for PKGBUILD consumption."""
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        archive_path.unlink()
    with tarfile.open(archive_path, "w:gz") as tf:
        for entry in sorted(patches_dir.iterdir()):
            if entry.is_file():
                tf.add(entry, arcname=entry.name)


def render_pkgbuild(template_path: Path, dest_path: Path, pkgver: str, srcname: str, sources: list[str], sha256sums: list[str]):
    """Fill PKGBUILD template placeholders and write to dest_path."""
    template = template_path.read_text()
    sources_block = "\n".join(f"    \"{s}\"" for s in sources)
    sha_block = "\n".join(f"    {s}" for s in sha256sums)
    content = (
        template
        .replace("{pkgver}", pkgver)
        .replace("{srcname}", srcname)
        .replace("{sources_block}", sources_block)
        .replace("{sha256sums_block}", sha_block)
    )
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(content)


def collect_commit_patches(repo: Path, commits: list[str], start_number: int, subjects: dict[str, str]) -> list[Patch]:
    """Materialize ordered commits into in-memory patches with deterministic labels."""
    collected: list[Patch] = []
    current = start_number
    for commit in commits:
        res = run_git(
            [
                "git",
                "-C",
                str(repo),
                "format-patch",
                "-1",
                commit,
                "--stdout",
            ],
            capture=True,
        )

        subject = subjects.get(commit, "")
        safe_subject = re.sub(r"[^A-Za-z0-9._-]+", "-", subject).strip("-") or commit[:12]
        collected.append((safe_subject, res.stdout))
        current += 1

    return collected


def simulate_apply(repo: Path, base_treeish: str, cachyos: list[Patch], tuxedo: list[Patch]) -> list[Patch]:
    """Apply patches against a temporary index without touching the working tree.

    CachyOS patches must all apply (or already be applied upstream); tuxedo patches are filtered
    to only those that apply in order. "Already applied" (reverse-clean) patches are skipped.
    Returns the list of patches that need applying in order.
    """
    env = os.environ.copy()
    with tempfile.NamedTemporaryFile(delete=False) as idx:
        index_path = idx.name

    env.update({
        "GIT_INDEX_FILE": index_path,
        "GIT_WORK_TREE": str(repo),
    })

    try:
        run_git(["git", "-C", str(repo), "read-tree", base_treeish], env=env)

        def apply_patch(label: str, content: str, allow_skip: bool = False) -> tuple[str, str]:
            args = [
                "git",
                "-C",
                str(repo),
                "apply",
                "--cached",
                "--whitespace=nowarn",
                "-",
            ]

            res = run_git(args, capture=True, check=False, stdin_data=content, env=env)
            if res.returncode == 0:
                return "applied", ""

            if allow_skip:
                reverse_check = run_git(
                    args + ["--reverse", "--check"],
                    capture=True,
                    check=False,
                    stdin_data=content,
                    env=env,
                )
                if reverse_check.returncode == 0:
                    return "skipped", "already applied (reverse clean)"

            res_three_way = run_git(args + ["--3way"], capture=True, check=False, stdin_data=content, env=env)
            if res_three_way.returncode == 0:
                return "applied", ""

            msg = (res_three_way.stderr or res_three_way.stdout or res.stderr or res.stdout or "apply failed").strip()
            return "failed", msg

        for label, content in cachyos:
            print(f"[applying CachyOS patch] {label}")
            status, msg = apply_patch(label, content, allow_skip=False)
            if status == "failed":
                raise RuntimeError(f"CachyOS patch failed to apply on {base_treeish}: {label} :: {msg}")
            if status == "skipped":
                print(f"[already applied] {label}: {msg}")

        applied_tuxedo: list[Patch] = []
        failed_tuxedo: list[tuple[str, str]] = []
        for label, content in tuxedo:
            print(f"[testing Tuxedo patch] {label}")
            status, msg = apply_patch(label, content, allow_skip=True)
            if status == "applied":
                applied_tuxedo.append((label, content))
            elif status == "skipped":
                print(f"[already applied] {label}: {msg}")
            else:
                failed_tuxedo.append((label, msg))

        if failed_tuxedo:
            print("[tuxedo skipped] patches that failed to apply:")
            for lbl, msg in failed_tuxedo:
                print(f"  - {lbl}: {msg}")

        return cachyos + applied_tuxedo
    finally:
        Path(index_path).unlink(missing_ok=True)


def write_patches(patches: list[Patch], dest_dir: Path, start_number: int = 1):
    """Write collected patches to dest_dir with sequential numbering, replacing existing contents."""
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    current = start_number
    for label, content in patches:
        filename = f"{current:04d}-{label}.patch"
        (dest_dir / filename).write_text(content)
        current += 1


def main():
    """Fetch kernel sources (single repo, multiple remotes) and collect commit hashes."""

    linux_base_ref = ensure_ref(LINUX_REPO, "origin", LINUX_REMOTE, LINUX_BRANCH, LINUX_REF, "linux")
    tuxedo_ref = ensure_ref(LINUX_REPO, "tuxedo", TUXEDO_REMOTE, TUXEDO_BRANCH, TUXEDO_REF, "tuxedo")
    ubuntu_ref = resolve_ubuntu_base(tuxedo_ref)

    ordered_tuxedo_unique = rev_list_range(
        str(LINUX_REPO),
        include=tuxedo_ref,
        exclude=[ubuntu_ref, linux_base_ref],
        reverse=True,
    )
    if EXCLUDE_COMMITS:
        ordered_tuxedo_unique = [h for h in ordered_tuxedo_unique if h not in EXCLUDE_COMMITS and h[:12] not in EXCLUDE_COMMITS]
    if len(ordered_tuxedo_unique) > 50:
        sys.exit(f"unexpected tuxedo patch count: {len(ordered_tuxedo_unique)} (>50)")
    tuxedo_subjects = bulk_subjects(str(LINUX_REPO), ordered_tuxedo_unique)

    for h in ordered_tuxedo_unique:
        print(f"{h} {tuxedo_subjects.get(h, '')}")
    
    if not CACHYOS_PKGBUILDS_REF:
        sys.exit("CACHYOS_PKGBUILDS_REF must be set")
    checkout(LINUX_CACHYOS_REPO, "origin", CACHYOS_PKGBUILDS_REMOTE, CACHYOS_PKGBUILDS_BRANCH, ref=CACHYOS_PKGBUILDS_REF)
    stage_cachyos_config(LINUX_CACHYOS_REPO / CACHYOS_PKG_DIR / "config", PACKAGE_CONFIG_PATH)


    if not CACHYOS_PATCHES_REF:
        sys.exit("CACHYOS_PATCHES_REF must be set")
    checkout(LINUX_CACHYOS_PATCHES_REPO, "origin", CACHYOS_PATCHES_REMOTE, CACHYOS_PATCHES_BRANCH, ref=CACHYOS_PATCHES_REF)
    cachyos_pkgbuild_path = LINUX_CACHYOS_REPO / CACHYOS_PKG_DIR / "PKGBUILD"
    cachyos_patches = collect_cachyos_patches(
        LINUX_CACHYOS_PATCHES_REPO,
        CACHYOS_PATCHES_FOLDER,
        cachyos_pkgbuild_path,
        lto_mode="thin",
        cpusched="bore",
    )

    tuxedo_patches = collect_commit_patches(
        LINUX_REPO,
        ordered_tuxedo_unique,
        start_number=len(cachyos_patches) + 1,
        subjects=tuxedo_subjects,
    )

    applied = simulate_apply(LINUX_REPO, linux_base_ref, cachyos_patches, tuxedo_patches)
    write_patches(applied, PACKAGE_PATCHES_DIR)

    patches_tarball = ROOT / "package" / "patches.tar.gz"
    create_patches_tarball(PACKAGE_PATCHES_DIR, patches_tarball)

    # Render PKGBUILD using scripts/PKGBUILD template
    srcname = KERNEL_SOURCE.rsplit("/", 1)[-1].rsplit(".", 2)[0]
    sources = [KERNEL_SOURCE, "config", "patches.tar.gz"]
    sha256sums = ["'SKIP'", "'SKIP'", "'SKIP'"]
    render_pkgbuild(PKGBUILD_TEMPLATE, PACKAGE_PKGFILE, PKGVER, srcname, sources, sha256sums)

    # Summary
    applied_tuxedo = max(0, len(applied) - len(cachyos_patches))
    skipped_tuxedo = len(tuxedo_patches) - applied_tuxedo
    print();
    print(f"[summary] cachyos patches: {len(cachyos_patches)}")
    print(f"[summary] tuxedo patches: applied {applied_tuxedo} of {len(tuxedo_patches)}, skipped {skipped_tuxedo}")
    print(f"[summary] Success!")

if __name__ == "__main__":
    main()
