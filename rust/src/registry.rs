//! The registry backend, for embedded consumers (forge-ci/cache#6).
//!
//! Mirrors `cachelib/store.py`'s `OciStore`: one registry repository per key, the
//! tag is the sequence number, the layer is a blob, and the manifest annotations
//! carry the fields a restore needs so a tag listing is enough to decide.
//!
//! Two things the Python side learned the hard way and are respected here:
//!
//! * the manifest `PUT`/`GET` must use a *manifest* media type —
//!   `application/vnd.oci.image.manifest.v1+json`. A custom `artifactType` as the
//!   Content-Type is a 400, and asking for it with `Accept` returns nothing, which
//!   makes every repository look empty;
//! * a manifest must not reference a blob the registry does not hold, so the empty
//!   config descriptor is uploaded before the tag.

use crate::{parse_seq, seq_name, Key, Manifest, ManifestError};
use sha2::{Digest, Sha256};
use std::fs;
use std::io::Write;
use std::path::Path;

pub const OCI_MANIFEST_TYPE: &str = "application/vnd.oci.image.manifest.v1+json";
pub const LAYER_TYPE: &str = "application/vnd.oci.image.layer.v1.tar+zstd";
pub const ARTIFACT_TYPE: &str = "application/vnd.forge-ci.cache.layer.v1";

/// Where the store lives. `scheme` is http for a localhost registry in tests.
#[derive(Debug, Clone)]
pub struct Registry {
    pub host: String,
    pub namespace: String,
    pub token: String,
    pub scheme: String,
}

impl Registry {
    pub fn new(host: &str, namespace: &str) -> Self {
        Registry {
            host: host.to_string(),
            namespace: namespace.to_string(),
            token: String::new(),
            scheme: "https".to_string(),
        }
    }

    pub fn plain_http(mut self) -> Self {
        self.scheme = "http".to_string();
        self
    }

    pub fn with_token(mut self, token: &str) -> Self {
        self.token = token.to_string();
        self
    }

    fn base(&self) -> String {
        format!("{}://{}", self.scheme, self.host)
    }
}

/// One registry repository per key, so a tag listing is the whole index.
pub fn repository_name(key: &Key, namespace: &str) -> String {
    let slug: String = format!("{}-{}-{}", key.repo, key.family, key.platform)
        .to_lowercase()
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() || c == '.' { c } else { '-' })
        .collect();
    let slug = slug.trim_matches('-').to_string();
    if namespace.is_empty() {
        slug
    } else {
        format!("{namespace}/{slug}")
    }
}

#[derive(Debug, Clone)]
pub struct OciStore {
    registry: Registry,
    key: Key,
    repository: String,
}

fn status_error(context: &str, error: ureq::Error) -> ManifestError {
    match error {
        ureq::Error::Status(code, response) => {
            let body = response.into_string().unwrap_or_default();
            ManifestError(format!("{context}: {code} {}", body.chars().take(300).collect::<String>()))
        }
        other => ManifestError(format!("{context}: {other}")),
    }
}

impl OciStore {
    pub fn new(registry: Registry, key: Key) -> Self {
        let repository = repository_name(&key, &registry.namespace);
        OciStore { registry, key, repository }
    }

    fn auth_header(&self) -> Option<String> {
        if self.registry.token.is_empty() {
            None
        } else {
            Some(format!("Bearer {}", self.registry.token))
        }
    }

    fn get(&self, path: &str, accept: &str) -> Result<String, ManifestError> {
        let url = format!("{}{path}", self.registry.base());
        let mut request = ureq::get(&url).set("Accept", accept);
        if let Some(auth) = self.auth_header() {
            request = request.set("Authorization", &auth);
        }
        match request.call() {
            Ok(response) => response
                .into_string()
                .map_err(|error| ManifestError(format!("{url}: {error}"))),
            Err(ureq::Error::Status(404, _)) => Err(ManifestError(format!("404 {path}"))),
            Err(error) => Err(status_error(path, error)),
        }
    }

    /// Every entry for the key, oldest first.
    pub fn entries(&self) -> Result<Vec<Manifest>, ManifestError> {
        let listing = match self.get(&format!("/v2/{}/tags/list", self.repository), "application/json") {
            Ok(body) => body,
            Err(_) => return Ok(Vec::new()),   // no repository yet is an empty store
        };
        let document: serde_json::Value = serde_json::from_str(&listing)?;
        let mut found: Vec<Manifest> = Vec::new();
        let tags = document.get("tags").and_then(|tags| tags.as_array());
        for tag in tags.into_iter().flatten() {
            let Some(name) = tag.as_str() else { continue };
            if parse_seq(name).is_err() {
                continue;
            }
            if let Some(manifest) = self.manifest_from_tag(name)? {
                found.push(manifest);
            }
        }
        found.sort_by_key(|manifest| manifest.seq);
        Ok(found)
    }

    fn manifest_from_tag(&self, tag: &str) -> Result<Option<Manifest>, ManifestError> {
        let body = match self.get(
            &format!("/v2/{}/manifests/{tag}", self.repository),
            OCI_MANIFEST_TYPE,
        ) {
            Ok(body) => body,
            Err(_) => return Ok(None),
        };
        let document: serde_json::Value = serde_json::from_str(&body)?;
        let annotation = |name: &str| {
            document
                .get("annotations")
                .and_then(|value| value.get(name))
                .and_then(|value| value.as_str())
                .unwrap_or("")
                .to_string()
        };
        let layer = document
            .get("layers")
            .and_then(|layers| layers.get(0))
            .ok_or_else(|| ManifestError(format!("{tag} has no layers")))?;
        let seq = parse_seq(tag)?;
        // An empty annotation means "no parent"; Some("") would fail validation
        // as a full layer with a parent, and the entry would silently vanish.
        let parent = {
            let value = annotation("org.forge-ci.cache.parent");
            if value.is_empty() { None } else { Some(value) }
        };
        Manifest::from_value(
            self.key.clone(),
            serde_json::json!({
                "seq": seq,
                "identity": annotation("org.opencontainers.image.identity"),
                "kind": annotation("org.forge-ci.cache.kind"),
                "parent": parent,
                "digest": layer.get("digest").and_then(|value| value.as_str()).unwrap_or(""),
                "bytes": layer.get("size").and_then(|value| value.as_u64()).unwrap_or(0),
                "created_at": annotation("org.opencontainers.image.created"),
                "ref": annotation("org.opencontainers.image.ref.name"),
                "source_sha": annotation("org.opencontainers.image.revision"),
                "run_id": annotation("org.forge-ci.cache.run-id"),
            }),
        )
        .map(Some)
    }

    /// Upload one layer's bytes and its tag.
    pub fn put_blob(&self, manifest: &Manifest, source: &Path) -> Result<(), ManifestError> {
        let data = fs::read(source)
            .map_err(|error| ManifestError(format!("{}: {error}", source.display())))?;
        let digest = crate::store::digest_of(source)?;
        if digest != manifest.digest {
            return Err(ManifestError(format!(
                "{} hashes to {digest}, manifest says {}",
                source.display(),
                manifest.digest
            )));
        }
        self.upload(&manifest.digest, &data)?;
        let config = b"{}".to_vec();
        let mut hasher = Sha256::new();
        hasher.update(&config);
        let config_digest = format!("sha256:{:x}", hasher.finalize());
        self.upload(&config_digest, &config)?;

        let document = serde_json::json!({
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST_TYPE,
            "artifactType": ARTIFACT_TYPE,
            "config": {
                "mediaType": "application/vnd.oci.empty.v1+json",
                "digest": config_digest,
                "size": config.len(),
            },
            "layers": [{ "mediaType": LAYER_TYPE, "digest": manifest.digest, "size": manifest.bytes }],
            "annotations": {
                "org.opencontainers.image.identity": manifest.identity,
                "org.forge-ci.cache.kind": manifest.kind,
                "org.forge-ci.cache.parent": manifest.parent.clone().unwrap_or_default(),
                "org.opencontainers.image.created": manifest.created_at,
                "org.opencontainers.image.ref.name": manifest.git_ref,
                "org.opencontainers.image.revision": manifest.source_sha,
                "org.forge-ci.cache.run-id": manifest.run_id,
            },
        });
        let tag = manifest.key.tag(manifest.seq)?;
        let url = format!("{}/v2/{}/manifests/{tag}", self.registry.base(), self.repository);
        let mut request = ureq::put(&url).set("Content-Type", OCI_MANIFEST_TYPE);
        if let Some(auth) = self.auth_header() {
            request = request.set("Authorization", &auth);
        }
        request
            .send_string(&document.to_string())
            .map(|_| ())
            .map_err(|error| status_error("manifest PUT", error))
    }

    fn upload(&self, digest: &str, data: &[u8]) -> Result<(), ManifestError> {
        let url = format!("{}/v2/{}/blobs/uploads/", self.registry.base(), self.repository);
        let mut request = ureq::post(&url);
        if let Some(auth) = self.auth_header() {
            request = request.set("Authorization", &auth);
        }
        let response = request
            .send_bytes(&[])
            .map_err(|error| status_error("upload POST", error))?;
        let location = response
            .header("Location")
            .ok_or_else(|| ManifestError("registry did not return an upload location".to_string()))?;
        let separator = if location.contains('?') { '&' } else { '?' };
        let mut target = format!("{location}{separator}digest={digest}");
        if target.starts_with('/') {
            target = format!("{}{target}", self.registry.base());
        }
        let mut upload = ureq::put(&target).set("Content-Type", "application/octet-stream");
        if let Some(auth) = self.auth_header() {
            upload = upload.set("Authorization", &auth);
        }
        upload
            .send_bytes(data)
            .map(|_| ())
            .map_err(|error| status_error(&format!("blob PUT {digest}"), error))
    }

    /// Download one layer, verifying the digest on the way.
    pub fn get_blob(&self, manifest: &Manifest, destination: &Path) -> Result<(), ManifestError> {
        let url = format!(
            "{}/v2/{}/blobs/{}",
            self.registry.base(),
            self.repository,
            manifest.digest
        );
        let mut request = ureq::get(&url);
        if let Some(auth) = self.auth_header() {
            request = request.set("Authorization", &auth);
        }
        let response = request
            .call()
            .map_err(|error| status_error("blob GET", error))?;
        if let Some(parent) = destination.parent() {
            fs::create_dir_all(parent)
                .map_err(|error| ManifestError(format!("{}: {error}", parent.display())))?;
        }
        let mut reader = response.into_reader();
        let mut file = fs::File::create(destination)
            .map_err(|error| ManifestError(format!("{}: {error}", destination.display())))?;
        std::io::copy(&mut reader, &mut file)
            .map_err(|error| ManifestError(format!("{}: {error}", destination.display())))?;
        file.flush()
            .map_err(|error| ManifestError(format!("{}: {error}", destination.display())))?;
        if crate::store::digest_of(destination)? != manifest.digest {
            let _ = fs::remove_file(destination);
            return Err(ManifestError(format!("blob {} arrived corrupt", manifest.digest)));
        }
        Ok(())
    }

    /// Publish through the same plan-then-write shape the filesystem store uses.
    pub fn publish(
        &self,
        identity: &str,
        stream: &Path,
        max_delta: usize,
    ) -> Result<Manifest, ManifestError> {
        let entries = self.entries()?;
        let plan = crate::plan_push(&entries, &self.key, identity, max_delta)?;
        let bytes = fs::metadata(stream)
            .map_err(|error| ManifestError(format!("{}: {error}", stream.display())))?
            .len();
        let manifest = Manifest::from_value(
            self.key.clone(),
            serde_json::json!({
                "seq": plan.seq,
                "identity": identity,
                "kind": plan.kind.as_str(),
                "parent": plan.parent,
                "digest": crate::store::digest_of(stream)?,
                "bytes": bytes,
                "created_at": crate::store::now(),
            }),
        )?;
        self.put_blob(&manifest, stream)?;
        Ok(manifest)
    }

    pub fn repository(&self) -> &str {
        &self.repository
    }

    pub fn key(&self) -> &Key {
        &self.key
    }

    /// The tag name a sequence number maps to, for callers that want to look.
    pub fn tag(seq: u32) -> Result<String, ManifestError> {
        seq_name(seq)
    }
}
