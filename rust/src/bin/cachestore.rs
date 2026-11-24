//! A thin CLI over the embedded store, so the two implementations can be
//! checked against each other from the outside.
//!
//!     cachestore push --store DIR --repo OWNER/NAME --family F --platform P \
//!                     --identity ID --stream FILE
//!     cachestore list --store DIR --repo ... --family F --platform P
//!     cachestore plan --store DIR ... --identity ID
//!     cachestore pull --store DIR ... --identity ID --dest DIR
//!
//! `tests/test_rust_interop.py` drives this binary against a store and then
//! reads the same store with the Python package (and the other way round), which
//! is the only way "the two implementations agree" is a fact rather than a hope.

use cachelib::{store::FileStore, Key};
use std::collections::HashMap;
use std::path::PathBuf;
use std::process::ExitCode;

struct Args {
    store: PathBuf,
    repo: String,
    family: String,
    platform: String,
    identity: String,
    stream: Option<PathBuf>,
    dest: Option<PathBuf>,
}

fn parse(args: &[String]) -> Result<(String, Args), String> {
    let mut command = String::new();
    let mut options: HashMap<String, String> = HashMap::new();
    let mut index = 0;
    while index < args.len() {
        let flag = args[index].as_str();
        if ["push", "list", "plan", "pull"].contains(&flag) {
            if !command.is_empty() {
                return Err(format!("two commands: {command} and {flag}"));
            }
            command = flag.to_string();
            index += 1;
            continue;
        }
        let name = flag
            .strip_prefix("--")
            .ok_or_else(|| format!("unknown argument {flag}"))?;
        let value = args
            .get(index + 1)
            .ok_or_else(|| format!("{flag} needs a value"))?;
        options.insert(name.to_string(), value.clone());
        index += 2;
    }
    if command.is_empty() {
        return Err("expected one of: push, list, plan, pull".to_string());
    }
    let take = |name: &str| options.get(name).cloned();
    Ok((
        command,
        Args {
            store: PathBuf::from(take("store").ok_or("--store is required")?),
            repo: take("repo").ok_or("--repo is required")?,
            family: take("family").ok_or("--family is required")?,
            platform: take("platform").ok_or("--platform is required")?,
            identity: take("identity").unwrap_or_default(),
            stream: take("stream").map(PathBuf::from),
            dest: take("dest").map(PathBuf::from),
        },
    ))
}

fn run() -> Result<(), String> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let (command, args) = parse(&args)?;
    let key = Key::new(&args.repo, &args.family, &args.platform).map_err(|e| e.to_string())?;
    let store = FileStore::open(&args.store);
    match command.as_str() {
        "push" => {
            let stream = args.stream.ok_or("push needs --stream")?;
            let manifest = store
                .publish(&key, &args.identity, &stream)
                .map_err(|e| e.to_string())?;
            println!(
                "published {} s{:06} {} ({} B) for {} identity {}",
                manifest.kind.as_str(),
                manifest.seq,
                manifest.digest,
                manifest.bytes,
                key.prefix(),
                args.identity
            );
        }
        "list" => {
            for manifest in store.entries(&key).map_err(|e| e.to_string())? {
                println!(
                    "s{:06}  {:5}  {:>12}  {}  {}",
                    manifest.seq,
                    manifest.kind.as_str(),
                    manifest.bytes,
                    manifest.identity,
                    manifest.created_at
                );
            }
        }
        "plan" => {
            let plan = store.plan(&key, &args.identity).map_err(|e| e.to_string())?;
            println!(
                "publish: {} s{:06} ({})",
                plan.kind.as_str(),
                plan.seq,
                plan.reason
            );
            if let Some(parent) = &plan.parent {
                println!("parent: {parent}");
            }
            let restore = store
                .restore_plan(&key, &args.identity)
                .map_err(|e| e.to_string())?;
            if restore.hit() {
                let tip = restore
                    .tip()
                    .map(|layer| format!("s{:06}", layer.seq))
                    .unwrap_or_default();
                let seqs: Vec<String> = restore
                    .layers
                    .iter()
                    .map(|layer| format!("s{:06}", layer.seq))
                    .collect();
                println!("restore: hit: {tip} via {} layer(s)", seqs.len());
                println!("layers: {}", seqs.join(","));
            } else {
                println!("restore: miss: {}", restore.reason);
            }
        }
        "pull" => {
            let dest = args.dest.ok_or("pull needs --dest")?;
            let restore = store
                .restore_plan(&key, &args.identity)
                .map_err(|e| e.to_string())?;
            if !restore.hit() {
                return Err(format!("no cache to pull: {}", restore.reason));
            }
            std::fs::create_dir_all(&dest).map_err(|e| format!("{}: {e}", dest.display()))?;
            for layer in &restore.layers {
                let target = dest.join(format!("layer-{:06}", layer.seq));
                store.get_blob(layer, &target).map_err(|e| e.to_string())?;
                println!("restored s{:06} -> {}", layer.seq, target.display());
            }
        }
        other => return Err(format!("unknown command {other}")),
    }
    Ok(())
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("cachestore: {error}");
            ExitCode::FAILURE
        }
    }
}
