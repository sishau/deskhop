// Zero-dependency build script: embeds icon.rc (compiled with MSVC rc.exe)
// into the executable so both the Explorer file icon and the tray icon come
// from resources. If rc.exe cannot be found, the build silently proceeds
// without an embedded icon (tray falls back to IDI_APPLICATION).
use std::path::{Path, PathBuf};
use std::process::Command;

fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed=icon.rc");
    println!("cargo:rerun-if-changed=icon.ico");

    let manifest = match std::env::var("CARGO_MANIFEST_DIR") {
        Ok(m) => m,
        Err(_) => return,
    };
    if !Path::new(&manifest).join("icon.rc").exists() {
        return;
    }

    let Some(rc) = find_rc() else {
        println!("cargo:warning=rc.exe not found; building without embedded icon");
        return;
    };

    let out = match std::env::var("OUT_DIR") {
        Ok(o) => o,
        Err(_) => return,
    };
    let res = Path::new(&out).join("icon.res");
    let status = Command::new(&rc)
        .args(["/nologo", "/fo"])
        .arg(&res)
        .arg(Path::new(&manifest).join("icon.rc"))
        .status();

    match status {
        Ok(s) if s.success() => {
            println!("cargo:rustc-link-arg-bins={}", res.display());
        }
        _ => println!("cargo:warning=rc.exe failed; building without embedded icon"),
    }
}

/// Locate rc.exe under the common Visual Studio / Windows SDK roots,
/// preferring the HostX64\\x64 flavor.
fn find_rc() -> Option<PathBuf> {
    let roots = [
        r"C:\Program Files (x86)\Microsoft Visual Studio",
        r"C:\Program Files\Microsoft Visual Studio",
        r"C:\Program Files (x86)\Windows Kits\10\bin",
    ];
    let mut best: Option<(u32, PathBuf)> = None;
    for root in roots {
        walk(Path::new(root), 0, &mut best);
    }
    best.map(|(_, p)| p)
}

fn score(p: &Path) -> u32 {
    let s = p.to_string_lossy().to_lowercase();
    if s.contains("hostx64\\x64") {
        2
    } else if s.contains("\\x64\\") {
        1
    } else {
        0
    }
}

fn walk(dir: &Path, depth: u32, best: &mut Option<(u32, PathBuf)>) {
    if depth > 12 {
        return;
    }
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for e in entries.flatten() {
        let p = e.path();
        if p.is_dir() {
            let name = p
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or("")
                .to_lowercase();
            if matches!(name.as_str(), "installer" | "shared" | "integration") {
                continue;
            }
            walk(&p, depth + 1, best);
        } else if p.file_name().and_then(|n| n.to_str()) == Some("rc.exe") {
            let s = score(&p);
            if best.as_ref().is_none_or(|(bs, _)| s > *bs) {
                *best = Some((s, p));
            }
        }
    }
}
