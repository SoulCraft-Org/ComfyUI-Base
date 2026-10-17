# 50-models.sh — the model library: rows, canonical dests, one index over every filesystem, move-or-download,
# exact sizes, the disk gate, one deletion prompt scoped by the ledger, and gen-models for the row's bytes.
#
# Row format (every package's MODELS): category|Family|Purpose|file|url|bytes|note|alts
#   dest      = category/Family[/Purpose]/file   (Family empty only for the freeform categories LLM, custom_nodes/<Pack>,
#               embeddings, sams, depthanything, SEEDVR2, RMBG and grounding-dino, which write category/[Purpose/]file;
#               vae_approx is flat: ComfyUI's preview loader wants the file right there)
#   url       = https://huggingface.co/<repo>/resolve/<revision>/<path>  |  https://github.com/...  (Hugging Face and GitHub only)
#             | LOCAL   (must already exist somewhere on the pod; relocated into place, else the run FAILS — a file, or
#             |          with a trailing / on file, a snapshot FOLDER of that name holding a config.json)
#             | hf://owner/repo[@<commit>]  with file ending in "/"  (a repo snapshot into that directory)
#             3.12.0: the revision is honoured. A 40-hex commit in a resolve URL, or hf://owner/repo@<commit>, pins the
#             row: what downloads is that commit's file, and every Hub file is checked against the sha256 the Hub states
#             for that revision before it is moved into place. `base.sh gen-models <dir> --pin` prints the rows pinned.
#   bytes     = exact size; 0 only with LOCAL (any size, must exist). `base.sh gen-models <dir>` fills it in.
#   alts      = space-separated legacy basenames the index may adopt for this file

BASE_MODEL_EXTS='*.safetensors *.pth *.pt *.pkl *.gguf *.ckpt *.bin *.onnx *.partial'
# What a staged deletion is renamed to while its destinations are re-checked. A run killed mid-stage leaves
# these behind, and `rescue` is what puts them back; they are never indexed as models.
BASE_PRUNE_SUFFIX='.comfy-base-pending'
# one prune set for every filesystem-wide walk: kernel/virtual trees, package caches, and the places that hold
# *.pth files which are path configs rather than weights
# lost+found is pruned from BOTH walks: every ext4 volume has one, it is mode 700 owned by nobody, and a
# root-squashed pod-local volume denies it even to uid 0 — find then exits 1 and the index is declared
# incomplete, whose remedy is re-downloading models that are already on disk (found on a 300 GB pod-local
# /workspace; the MooseFS network volume has no lost+found, which is why this never showed up before).
# 2.0.53: /tmp and /var/tmp, the Linux temp roots — the Mac's (/var/folders) was already here. The base's own
# install test writes zero-filled files of each row's declared size under pytest's /tmp tree; a pod whose library
# lacked a LoRA then MOVED 862 MB of those zeros in as the model, and the render died on its header. An exact
# -path, never a prefix: a test's own walk starts INSIDE its fake root (under /tmp on Linux), so only a real run's
# walk from / ever passes through /tmp itself.
BASE_PRUNE=( '(' -path /proc -o -path /sys -o -path /dev -o -path /run -o -path /var/lib/docker -o -path /snap
             -o -path /System -o -path /Volumes -o -path /Library -o -path /nix -o -path /var/folders -o -path /tmp -o -path /var/tmp
             -o -name lost+found -o -name .git -o -name '.venv*' -o -name node_modules -o -name __pycache__
             -o -name site-packages -o -name dist-packages -o -name .comfy-base-staging -o -name comfy-base ${BASE_FAKE_IMAGE_ROOT:+-o} ${BASE_FAKE_IMAGE_ROOT:+-path} ${BASE_FAKE_IMAGE_ROOT:+"$BASE_FAKE_IMAGE_ROOT"} ')' )
# the directory walk keeps .venv* visible (it is a search target, not noise)
BASE_PRUNE_DIRS=( '(' -path /proc -o -path /sys -o -path /dev -o -path /run -o -path /var/lib/docker -o -path /snap
                  -o -path /System -o -path /Volumes -o -path /Library -o -path /nix -o -path /var/folders -o -path /tmp -o -path /var/tmp
                  -o -name lost+found -o -name .git -o -name node_modules -o -name __pycache__
                  -o -name site-packages -o -name dist-packages -o -name .comfy-base-staging ${BASE_FAKE_IMAGE_ROOT:+-o} ${BASE_FAKE_IMAGE_ROOT:+-path} ${BASE_FAKE_IMAGE_ROOT:+"$BASE_FAKE_IMAGE_ROOT"} ')' )
BASE_DIRSCAN=""; IDX=""; IDX_ROOTS=""
_base_cache_prune(){ # → a find prune expression for the tool caches (3.5.0): the uv, pip, Hugging Face and xet caches hold
  # no ComfyUI tree, no pack and no model-shaped file (the Hub cache keeps blobs under hash names), only walk time
  local p out=() first=1
  for p in "${UV_CACHE_DIR:-}" "${PIP_CACHE_DIR:-}" "${HF_HOME:-}" "${HF_XET_CACHE:-}"; do
    [ -n "$p" ] || continue
    if [ "$first" = 1 ]; then out+=( -path "$p" ); first=0; else out+=( -o -path "$p" ); fi
  done
  [ "${#out[@]}" -gt 0 ] && printf '%s\n' '(' "${out[@]}" ')' || true
}
BASE_DL_BYTES=0; BASE_DL_TIME=0; BASE_DL_SECS=0

# ---------------------------------------------------------------- rows and dests
_base_model_rows(){ # prints validated MODELS rows; exit 1 on any violation (message on stderr)
  local row cat fam purp file url bytes note alts bad=0 seen=" "
  for row in ${MODELS[@]+"${MODELS[@]}"}; do
    IFS='|' read -r cat fam purp file url bytes note alts <<< "$row"
    if [ -z "$cat" ] || [ -z "$file" ] || [ -z "$url" ]; then echo "  !! bad MODELS row (want category|Family|Purpose|file|url|bytes|note|alts): $row" >&2; bad=1; continue; fi
    case "$cat" in
      LLM|custom_nodes/*|vae_approx|embeddings|sams|depthanything|SEEDVR2|RMBG|grounding-dino|latent_upscale_models|insightface|ultralytics/bbox|ultralytics/segm|Wan|DreamID-V|MiniMax-Remover) if [ -n "$fam" ]; then echo "  !! $cat is a freeform category: leave Family empty ($file)" >&2; bad=1; continue; fi;;   # flat by the consumer's design: embedding:<name> is resolved in the folder root; SAM3 SmartInpainter, DepthAnythingV2 and SeedVR2 os.listdir their folders; ComfyUI-RMBG reads models/RMBG/<name>/ and LayerStyle models/grounding-dino/ (2.0.17); the latent-upscaler packs glob *.safetensors at the root of latent_upscale_models/ and return basenames, so a Family folder hides the file from them (2.0.37); insightface's FaceAnalysis(root=X) reads X/models/<pack>/ by its own convention, so the category root is the root every package shares (2.0.39); ultralytics/bbox and /segm: face-detector packs (Impact's subpack, H3-FaceRefine) list models/ultralytics/bbox/<name> and join that path themselves, so a Family folder hides the detector from their combo (3.20.0); Wan and DreamID-V: DreamID-V's ComfyUI node joins models/Wan/Wan2.1-T2V-1.3B/ (a snapshot) and models/DreamID-V/<ckpt> itself (3.20.0); MiniMax-Remover: its loader reads models/MiniMax-Remover/{vae,transformer,scheduler}/ (diffusers folders, set by Purpose) and only fetches from the Hub when that folder is missing (3.20.0)
      *) if [ -z "$fam" ]; then echo "  !! $file needs a Family (the folder under $cat/; one spelling per family across the brand's packages, see <brand>/families.txt)" >&2; bad=1; continue; fi
         if ! [[ "$fam" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then echo "  !! Family '$fam' for $file must be a plain folder name (letters, digits, . _ -)" >&2; bad=1; continue; fi;;
    esac
    if ! [[ "$bytes" =~ ^[0-9]+$ ]]; then echo "  !! bytes must be an integer ($file: '$bytes')" >&2; bad=1; continue; fi
    case "$url" in
      LOCAL) ;;
      hf://*/*) if [[ "$file" != */ ]]; then echo "  !! an hf:// url is a repo snapshot: end the file with '/' ($file)" >&2; bad=1; continue; fi
                if ! [[ "$url" =~ ^hf://[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*(@[0-9a-f]{40})?$ ]]; then
                  echo "  !! an hf:// url is hf://owner/repo, pinned as hf://owner/repo@<40-hex commit> ($url)" >&2; bad=1; continue; fi;;
      https://huggingface.co/*|https://github.com/*) ;;
      *) echo "  !! url host not on the privacy list (huggingface.co, github.com) or not LOCAL/hf://: $url" >&2; bad=1; continue;;
    esac
    if [ "$bytes" = "0" ] && [ "$url" != "LOCAL" ]; then echo "  !! bytes=0 is only allowed with url=LOCAL ($file) — run: base.sh gen-models" >&2; bad=1; continue; fi
    case "$seen" in *" $(_base_lower "$file") "*) echo "  !! $file is declared twice" >&2; bad=1; continue;; esac
    seen="$seen$(_base_lower "$file") "; echo "$row"
  done
  return $bad
}
_base_dest_rel(){ # <row> → category/Family[/Purpose]/file
  local cat fam purp file rest
  IFS='|' read -r cat fam purp file rest <<< "$1"
  local p="$cat"; [ -n "$fam" ] && p="$p/$fam"; [ -n "$purp" ] && p="$p/$purp"
  echo "$p/$file"
}
_base_dest_path(){ # <dest rel> → absolute path (custom_nodes/<Pack>/… is relative to the located pack dir)
  local rel="$1"
  case "$rel" in
    custom_nodes/*)
      local rest="${rel#custom_nodes/}" pname sub e
      pname="${rest%%/*}"; sub="${rest#*/}"
      for e in ${PACK_DIRS[@]+"${PACK_DIRS[@]}"}; do if [ "${e%%|*}" = "$pname" ]; then echo "${e#*|}/$sub"; return 0; fi; done
      echo "$CN/$rest";;
    *) echo "$M/$rel";;
  esac
}

# ---------------------------------------------------------------- one walk for directories, one index for files
_base_scan_roots(){ # where models and stray trees are looked for: the WHOLE POD, the fake root in tests, every
  # filesystem off-pod. BASE_SEARCH_ROOTS overrides.
  #
  # 2.0.50: this used to return the volume alone on a pod, reasoning that the container disk is restored from the
  # image at every start, so a file adopted there is gone at the next stop. That is an argument about ADOPTING, not
  # about SEARCHING — and it made a model the image already ships invisible, so a 24 GiB row was downloaded when a
  # move would have taken seconds. Searching everywhere is safe because _base_move_into_place MOVES a complete file
  # onto the volume (mv on one device, else copy-verify-delete, never a symlink), which is exactly what the old
  # reasoning wanted to guarantee. Measured on a bare pod: the full walk with BASE_PRUNE costs 0.32 s.
  if [ -n "${BASE_SEARCH_ROOTS:-}" ]; then echo "$BASE_SEARCH_ROOTS"
  elif [ -n "$BASE_FAKE_ROOT" ]; then echo "$BASE_FAKE_ROOT"
  else echo /; fi
}
_base_dirscan(){ # ONE filesystem walk for every directory question the run asks
  if [ -n "$BASE_DIRSCAN" ] && [ -f "$BASE_DIRSCAN" ]; then return 0; fi
  BASE_DIRSCAN="$(_base_mktemp_d)/dirs.txt"
  local roots=() pats=() expr=() p nm rows
  read -r -a roots <<<"$(_base_scan_roots)"
  rows="$(_base_pack_rows 2>/dev/null || true)"
  while IFS='|' read -r nm _; do [ -n "$nm" ] && pats+=("$nm") || true; done <<< "$rows"
  pats+=('ComfyUI' 'ComfyUI-*' 'comfyui' '.venv' '.venv-*' '*.pre-*')
  local _c _f _p fl _r                                             # snapshot rows (file ends in /): their folder names are searched too
  while IFS='|' read -r _c _f _p fl _r; do case "$fl" in */) pats+=("${fl%/}");; esac; done <<< "$(_base_model_rows 2>/dev/null || true)"
  for nm in "${pats[@]}"; do
    if [ "${#expr[@]}" -eq 0 ]; then expr=( -name "$nm" ); else expr+=( -o -name "$nm" ); fi
  done
  # bounded: a hung network mount must not stall the run. `cd && pwd -P` collapses SYMLINKS. It does NOT
  # collapse a second mount of the same export: two mountpoints give one inode two different path strings,
  # and this walk indexes both. That is why the duplicate sweep compares dev:ino and not paths alone.
  local cp=(); while IFS= read -r p; do cp+=("$p"); done < <(_base_cache_prune)
  [ "${#cp[@]}" -gt 0 ] && cp+=( -prune -o ) || true
  if _base_timeout "${BASE_SCAN_SECS:-240}" \
    find "${roots[@]}" ${cp[@]+"${cp[@]}"} ${BASE_PRUNE_DIRS[@]+"${BASE_PRUNE_DIRS[@]}"} -prune -o -type d \( "${expr[@]}" \) -print 2>/dev/null \
    | while IFS= read -r p; do if [ -d "$p" ]; then (cd "$p" 2>/dev/null && pwd -P) || true; fi; done \
    | sort -u > "$BASE_DIRSCAN"; then :
  else
    printf '  !! the directory scan did not finish within %ss -- stray packs and venvs may go unreported\n' "${BASE_SCAN_SECS:-240}" >&2
    BASE_WARN+=("directory scan incomplete: raise BASE_SCAN_SECS if this tree is large or on a slow mount")
  fi
  return 0
}
_base_find_dirs(){ # <dir-name-glob>… → matches out of the single scan above
  _base_dirscan
  local d b p
  while IFS= read -r d; do
    [ -n "$d" ] || continue
    b="${d##*/}"                                           # 3.5.0: no basename fork per line (thousands on a used volume)
    for p in "$@"; do case "$b" in $p) echo "$d"; break;; esac; done
  done < "$BASE_DIRSCAN"
  return 0
}
_base_build_index(){ # every model-shaped file on every mounted filesystem → IDX as "size<TAB>path"
  IDX="$(_base_mktemp_d)/index.tsv"; : > "$IDX"
  local roots=() kept=() r k skip
  read -r -a roots <<<"$(_base_scan_roots)"                 # the volume on a pod; every filesystem off-pod
  while IFS= read -r r; do
    if [ -z "$r" ] || [ ! -d "$r" ]; then continue; fi
    skip=0; for k in ${kept[@]+"${kept[@]}"}; do case "$r/" in "$k"/*) skip=1;; esac; done
    if [ "$skip" = "0" ]; then kept+=("$r"); fi
  done < <(printf '%s\n' "${roots[@]}" | sed -E 's#(.)/+$#\1#' | awk 'NF' | sort -u)   # keep "/" itself
  IDX_ROOTS="${kept[*]}"
  local fmt=(-c $'%s\t%n'); [ "$BASE_STAT" = bsd ] && fmt=(-f $'%z\t%N')
  local names=() e first=1
  for e in $BASE_MODEL_EXTS; do if [ "$first" = 1 ]; then names=( -name "$e" ); first=0; else names+=( -o -name "$e" ); fi; done
  # No -xdev: models routinely sit on a different mount from where the walk starts. custom_nodes is NOT pruned
  # (rife426.pth lives inside a pack); site-packages IS (thousands of *.pth path files that are not weights).
  [ -n "$BASE_FAKE_ROOT" ] || note "indexing ${kept[*]} — a minute or two on a network volume"
  local cp=() p; while IFS= read -r p; do cp+=("$p"); done < <(_base_cache_prune)
  [ "${#cp[@]}" -gt 0 ] && cp+=( -prune -o ) || true
  if find "${kept[@]}" ${cp[@]+"${cp[@]}"} ${BASE_PRUNE[@]+"${BASE_PRUNE[@]}"} -prune -o -type f \( "${names[@]}" \) -print0 2>/dev/null \
    | _base_xargs0 stat "${fmt[@]}" 2>/dev/null >> "$IDX"; then :
  else
    # a short index is indistinguishable from "no models on disk", and the answer to that is a re-download
    printf '  !! the model index walk did not complete -- files already on disk may be fetched again\n' >&2
    BASE_WARN+=("model index incomplete: the filesystem walk failed, so downloads may repeat work already done")
  fi
  sort -u -t$'\t' -k2 "$IDX" -o "$IDX"
}
_base_idx_find(){ # <basename>... → "size<TAB>path" lines with an exact (case-insensitive) basename match
  local names="" n
  for n in "$@"; do if [ -n "$n" ]; then names="${names}${names:+|}$(_base_lower "$n")"; fi; done
  [ -n "$names" ] || return 0
  awk -F'\t' -v names="$names" 'BEGIN{n=split(names,a,"|"); for(i=1;i<=n;i++) want[a[i]]=1}
       { p=$2; sub(/.*\//,"",p); if (tolower(p) in want) print }' "$IDX"
}

# ---------------------------------------------------------------- the Hub: one parser, one auth, one error reader (3.12.0)
_base_hf_ref(){ # <url> → "repo|revision|path" for a Hub resolve URL or an hf:// snapshot; revision empty = the default branch
  local u="$1" repo rev="" path=""
  case "$u" in
    hf://*) repo="${u#hf://}"; case "$repo" in *@*) rev="${repo#*@}"; repo="${repo%%@*}";; esac;;
    https://huggingface.co/*/resolve/*) repo="${u#https://huggingface.co/}"; repo="${repo%%/resolve/*}"
                                        rev="${u#*/resolve/}"; path="${rev#*/}"; rev="${rev%%/*}";;
    *) return 1;;
  esac
  echo "$repo|$rev|$path"
}
_base_hub_auth(){ # <url> → BASE_HUB_AUTH=( -H @<0600 file> ) for a Hub URL when HF_TOKEN is set, else empty. The token
  # rides a header FILE, never curl's argv: argv is on the process list, readable by every user on the machine (3.12.0;
  # the size and sha256 HEADs had put it there since 2.0, against §0.9).
  BASE_HUB_AUTH=()
  [[ "$1" == https://huggingface.co/* ]] && [ -n "${HF_TOKEN:-}" ] || return 0
  [ -n "${BASE_TMPD:-}" ] || _base_tmp
  local f; f="$(_base_auth_file HF_TOKEN)" || return 0
  BASE_HUB_AUTH=(-H "@$f")
}
_base_hf_permanent(){ # <repo> <tool output> → 0 and one plain line when retrying cannot help (gated, token refused, not
  # found); 1 otherwise. A transient error is retried; these were retried three times, ten seconds of nothing (3.12.0).
  local repo="$1" out="$2"
  case "$out" in
    *"requires approval"*|*"gated repo"*|*"Access denied"*|*"is restricted"*)
      if [ -n "${HF_TOKEN:-}" ]; then err "$repo is gated: accept its licence at https://huggingface.co/$repo with the account HF_TOKEN belongs to, then re-run"
      else err "$repo is gated: accept its licence at https://huggingface.co/$repo, then give the install a read-only HF_TOKEN of that account and re-run"; fi
      return 0;;
    *"401 Client Error"*|*"Unauthorized"*|*"Invalid credentials"*|*"Invalid user token"*)
      err "the Hub refused HF_TOKEN for $repo (401): make a new read-only token at https://huggingface.co/settings/tokens"; return 0;;
    *"Revision not found"*|*"Revision Not Found"*)
      err "$repo has no such revision: the row's pin is wrong (base.sh gen-models <dir> --pin prints the current one)"; return 0;;
    *"Repository not found"*|*"Repository Not Found"*|*"File not found"*|*"Entry Not Found"*|*"404 Client Error"*)
      err "not on the Hub at the row's revision ($repo): the repository or the file moved, or the row is wrong"; return 0;;
  esac
  return 1
}
_base_hf_verify(){ # <hf> <repo> <revision> <dir> [--fail-on-missing-files] → 0 when every Hub file under <dir> matches the
  # sha256 (LFS) or git hash the Hub states for that revision. `hf cache verify` is the Hub's own check; an hf too old to
  # have it verifies nothing and says so (the size check still stands).
  local hfbin="$1" repo="$2" rev="${3:-main}" dir="$4" out; shift 4
  if ! "$hfbin" cache verify --help >/dev/null 2>&1; then note "sha256 not checked: this hf has no 'cache verify' (the size was)"; return 0; fi
  if out="$("$hfbin" cache verify "$repo" --revision "$rev" --local-dir "$dir" "$@" 2>&1)"; then return 0; fi
  printf '%s\n' "$out" | grep -vi 'do not exist on the remote' | tail -6
  return 1
}
# The record of what was verified (3.12.0): state/verified.tsv, "<dest>\t<commit>\t<etag>", appended, the last line per
# dest wins. A file is hashed ONCE, when it downloads; every later run takes its size and this record and hashes nothing.
# The commit and etag come from the metadata `hf download --local-dir` writes anyway, so recording costs no request.
_base_verified_add(){ # <dest> <commit> [etag]
  [ -n "${BASE_STATE:-}" ] || return 0
  mkdir -p "$BASE_STATE"; printf '%s\t%s\t%s\n' "$1" "$2" "${3:--}" >> "$BASE_STATE/verified.tsv"
}
_base_verified_get(){ # <dest> → "<commit>\t<etag>", or nothing
  [ -n "${BASE_STATE:-}" ] && [ -f "$BASE_STATE/verified.tsv" ] || return 0
  awk -F'\t' -v d="$1" '$1==d{r=$2"\t"$3} END{if(r!="")print r}' "$BASE_STATE/verified.tsv"
}
_base_verified_meta(){ # <dest> <hf .metadata file> → recorded (commit on its first line, etag on its second)
  local c e
  { read -r c; read -r e; } < "$2" 2>/dev/null || return 0
  if [[ "$c" =~ ^[0-9a-f]{40}$ ]]; then _base_verified_add "$1" "$c" "$e"; fi
  return 0
}
_base_pin_moved(){ # <dest> <url> → 0 when the row pins a commit other than the one this file was verified at AND the Hub's
  # file there differs. Only then is anything fetched or hashed again: an unpinned row, a file never recorded (adopted, or
  # from before 3.12.0) and a pin at the recorded commit cost nothing; a moved pin costs one HEAD (a snapshot: hf fetches
  # only the files that changed, then they are verified).
  local nrev rec rcommit retag h
  IFS='|' read -r _ nrev _ <<< "$(_base_hf_ref "$2")"
  [[ "$nrev" =~ ^[0-9a-f]{40}$ ]] || return 1
  rec="$(_base_verified_get "$1")"; [ -n "$rec" ] || return 1
  IFS=$'\t' read -r rcommit retag <<< "$rec"
  [ "$rcommit" != "$nrev" ] || return 1
  case "$2" in hf://*) return 0;; esac
  [ "$BASE_NO_NET" != "1" ] || return 1
  h="$(_base_hub_sha256 "$2")"
  [ -n "$h" ] || return 1                                 # the Hub states no sha256 here: the size stands, as before
  if [ "$h" = "$retag" ]; then _base_verified_add "$1" "$nrev" "$retag"; return 1; fi
  return 0
}

# ---------------------------------------------------------------- sizes
_base_expected(){ # <url> → exact:<bytes> from the Hub (x-linked-size beats content-length; a non-2xx final hop counts for nothing) | approx:0
  local url="$1" out xl cl st
  _base_hub_auth "$url"
  out="$(curl -sIL --max-time 40 --retry 2 ${BASE_HUB_AUTH[@]+"${BASE_HUB_AUTH[@]}"} "$url" 2>/dev/null | tr -d '\r' || true)"
  xl="$(printf '%s\n' "$out" | awk 'tolower($1)=="x-linked-size:"{v=$2} END{print v}')"
  cl="$(printf '%s\n' "$out" | awk '/^HTTP\//{c=""} tolower($1)=="content-length:"{c=$2} END{print c}')"
  st="$(printf '%s\n' "$out" | awk '/^HTTP\//{s=$2} END{print s}')"
  if [[ "$xl" =~ ^[0-9]+$ ]] && [ "$xl" -gt 0 ]; then echo "exact:$xl"
  elif [[ "$st" =~ ^2[0-9][0-9]$ ]] && [[ "$cl" =~ ^[0-9]+$ ]] && [ "$cl" -gt 0 ]; then echo "exact:$cl"
  else echo "approx:0"; fi
}
_base_size_ok(){ [[ "$1" =~ ^[0-9]+$ ]] || return 1; [ "$2" = "0" ] || [ "$1" = "$2" ]; }   # <actual> <bytes>; 0 = any size (LOCAL)
_base_dir_size(){ # bytes under a snapshot dir, minus the .cache/ metadata `hf download --local-dir` writes beside the files
  # and minus __pycache__: a pack that imports code from its snapshot (RMBG's birefnet.py) leaves one after the first render (2.0.25)
  "$SYS_PY" - "$1" <<'PYDS' 2>/dev/null || echo 0
import os, sys
total = 0
for dp, ds, fs in os.walk(sys.argv[1]):
    ds[:] = [d for d in ds if d not in (".cache", "__pycache__")]
    total += sum(os.path.getsize(os.path.join(dp, f)) for f in fs)
print(total)
PYDS
}
_base_sparse(){ "$SYS_PY" -c 'import sys; f=open(sys.argv[1],"wb"); f.truncate(int(sys.argv[2])); f.close()' "$1" "$2"; }

_base_move_into_place(){ # <src> <dest> — same device: mv; else copy + size check + delete. Never a symlink.
  local src="$1" dest="$2" want; want="$(_base_fsize "$src")"
  mkdir -p "$(dirname "$dest")"
  if [ "$(_base_fdev "$src")" = "$(_base_fdev "$(dirname "$dest")")" ]; then mv "$src" "$dest"; return 0; fi
  cp "$src" "$dest.basecopy" || { rm -f "$dest.basecopy"; return 1; }
  if [ "$(_base_fsize "$dest.basecopy")" = "$want" ]; then mv "$dest.basecopy" "$dest" && rm -f "$src"; else rm -f "$dest.basecopy"; return 1; fi
}
_base_rate(){ local b="$1" secs="$2"; if [ "${secs:-0}" -gt 0 ]; then awk -v b="$b" -v s="$secs" 'BEGIN{ printf "%.0f MB/s in %ds", b/1048576/s, s }'; else echo "<1s"; fi; }
_base_xet_check(){ # a silent fallback to plain HTTPS is several times slower and says nothing anywhere
  local hpy; hpy="$(dirname "$(_base_hf_bin)")/python"      # 3.11.0: the interpreter of the hf that downloads, not the main venv's
  if [ -x "$hpy" ] && "$hpy" -c "import hf_xet" 2>/dev/null; then ok "hf_xet active (high performance; its concurrency is adaptive)"
  else warn "hf_xet is NOT importable — downloads fall back to plain HTTPS, several times slower"; fi
}
_base_curl_dl(){ # <staged file> <url> [curl args] — the GitHub leg: curl, resumable; silent on success, its last lines on failure
  local staged="$1" url="$2" out; shift 2; out="$(mktemp)"
  if curl -fL --retry 3 -C - --max-time 3600 "$@" -o "$staged" "$url" > "$out" 2>&1; then rm -f "$out"; return 0; fi
  tail -3 "$out"; rm -f "$out"; return 1
}
_base_download(){ # <url> <dest> <bytes> → 0 on a verified file in place; sets BASE_DL_SECS
  local url="$1" dest="$2" bytes="$3" base stage staged rc=1 attempt t0=$SECONDS rfile=""
  base="$(basename "$dest")"; stage="$STAGING/$base.d"; mkdir -p "$stage"
  if [ "$BASE_NO_NET" = "1" ] && [ "${BASE_FAKE_DL:-}" != "real" ]; then   # BASE_FAKE_DL=real: the real branches with stubbed tools on PATH
    if [ "${BASE_FAKE_DL:-}" = "fail" ]; then err "fake download failed (BASE_FAKE_DL=fail)"; rm -rf "$stage"; return 1; fi
    staged="$stage/$base"; _base_sparse "$staged" "$bytes"      # fake: a sparse file at the declared size
  elif [[ "$url" == https://huggingface.co/*/resolve/* ]]; then
    local repo rev hfbin _dlout rargs=()
    IFS='|' read -r repo rev rfile <<< "$(_base_hf_ref "$url")"
    [ -n "$rev" ] && rargs=(--revision "$rev")      # 3.12.0: the URL's revision is the one fetched (it was dropped: always main)
    hfbin="$(_base_hf_bin)"                         # 3.11.0: the fetch venv's hf, else the main venv's, else PATH's
    if [ -z "$hfbin" ]; then err "hf CLI missing — the venv step installs huggingface_hub[hf-xet]"; return 1; fi
    for attempt in 1 2 3; do
      # silent on success (the caller reports file, size, verification and rate); the output is the diagnosis on failure
      # the token reaches hf through HF_TOKEN in its environment (exported by base_tokens), never as an argument
      if _dlout="$("$hfbin" download "$repo" "$rfile" ${rargs[@]+"${rargs[@]}"} --local-dir "$stage" --quiet 2>&1)"; then
        staged="$stage/$rfile"; [ -f "$staged" ] && break
      else
        printf '%s\n' "$_dlout" | tail -5
        if _base_hf_permanent "$repo" "$_dlout"; then break; fi
      fi
      [ "$attempt" = 3 ] || { note "attempt $attempt failed — retrying (hf download resumes)"; sleep 5; }
    done
    staged="$stage/$rfile"
    # the Hub's own sha256 for this revision, before the file goes anywhere (3.12.0); a mismatch is deleted, never resumed
    if [ -f "$staged" ] && _base_size_ok "$(_base_fsize "$staged")" "$bytes" && ! _base_hf_verify "$hfbin" "$repo" "$rev" "$stage"; then
      err "$base does not match the sha256 the Hub states for ${rev:-main}: deleted (a corrupt transfer, or the file changed on the Hub; pin the row)"
      rm -rf "$stage"; return 1
    fi
  else
    staged="$stage/$base"      # github releases and the like: curl, resumable
    for attempt in 1 2 3; do
      _base_curl_dl "$staged" "$url" && break
      [ "$attempt" = 3 ] || { note "attempt $attempt failed — retrying (the transfer resumes)"; sleep 5; }
    done
  fi
  if [ -f "$staged" ] && _base_size_ok "$(_base_fsize "$staged")" "$bytes"; then
    mkdir -p "$(dirname "$dest")"
    if mv "$staged" "$dest"; then
      [ -z "${rfile:-}" ] || _base_verified_meta "$dest" "$stage/.cache/huggingface/download/$rfile.metadata"   # a Hub file, verified above
      rm -rf "$stage"; rc=0
    fi
    BASE_DL_SECS=$(( SECONDS - t0 ))
  else
    [ -f "$staged" ] && err "staged file has $(_base_fsize "$staged") bytes, expected $bytes — kept in $stage for resume"
  fi
  return $rc
}
_base_snapshot(){ # <hf://owner/repo[@commit]> <dest dir> <bytes> → a repo snapshot in place, verified by total size and by
  # the Hub's own hashes for that revision (3.12.0)
  local repo rev dest="$2" bytes="$3" hfbin attempt rargs=() ok=0
  IFS='|' read -r repo rev _ <<< "$(_base_hf_ref "$1")"
  [ -n "$rev" ] && rargs=(--revision "$rev")
  mkdir -p "$dest"
  if [ "$BASE_NO_NET" = "1" ]; then _base_sparse "$dest/.fake-snapshot" "$bytes"; return 0; fi
  hfbin="$(_base_hf_bin)"
  if [ -z "$hfbin" ]; then err "hf CLI missing"; return 1; fi
  local _dlout
  for attempt in 1 2 3; do                          # the token rides HF_TOKEN in hf's environment, never argv (2.0.9); its output is the diagnosis on failure
    if _dlout="$("$hfbin" download "$repo" ${rargs[@]+"${rargs[@]}"} --local-dir "$dest" --quiet 2>&1)"; then ok=1; break; fi
    printf '%s\n' "$_dlout" | tail -5
    if _base_hf_permanent "$repo" "$_dlout"; then break; fi
    [ "$attempt" = 3 ] || { note "attempt $attempt failed — retrying"; sleep 5; }
  done
  [ "$ok" = 1 ] || return 1
  if [ "$(_base_dir_size "$dest")" != "$bytes" ]; then
    err "${1#hf://}: $(_base_dir_size "$dest") bytes on disk, the row says $bytes (an unpinned snapshot follows main: pin it)"; return 1
  fi
  if ! _base_hf_verify "$hfbin" "$repo" "$rev" "$dest" --fail-on-missing-files; then
    err "${1#hf://} does not match the hashes the Hub states for ${rev:-main}: kept in $dest for a resumed re-run"; return 1
  fi
  local meta; meta="$(find "$dest/.cache/huggingface/download" -name '*.metadata' 2>/dev/null | head -1)"
  [ -z "$meta" ] || _base_verified_meta "$dest" "$meta"
  return 0
}
_base_add_gb(){ awk -v a="$1" -v b="$2" 'BEGIN{printf "%.2f", a+b}'; }
_base_move_dir_into_place(){ # <src dir> <dest dir> → mv on one device, else copy, verify the byte total, remove the source
  local src="$1" dest="$2" want
  mkdir -p "$(dirname "$dest")"
  if [ "$(_base_fdev "$src")" = "$(_base_fdev "$(dirname "$dest")")" ]; then mv "$src" "$dest"; return $?; fi
  want="$(_base_dir_size "$src")"
  rm -rf "$dest.partial"; cp -a "$src" "$dest.partial" || { rm -rf "$dest.partial"; return 1; }
  if [ "$(_base_dir_size "$dest.partial")" = "$want" ]; then mv "$dest.partial" "$dest" && rm -rf "$src"; else rm -rf "$dest.partial"; return 1; fi
}

# ---------------------------------------------------------------- the disk gate: before anything is downloaded
_base_volume_quota_gb(){ # the volume size podctl recorded (state/volume.env), or nothing
  local f="$BASE_STATE/volume.env"
  [ -f "$f" ] && sed -n "s/^VOLUME_GB='\{0,1\}\([0-9]*\)'\{0,1\}$/\1/p" "$f" | head -1
  return 0
}
_base_volume_used_kb(){ # what the filesystem the disk gate judges holds, in kB (the suite sets BASE_FAKE_USED_KB)
  # 2.4.0: the gate judges the LIBRARY ($M), and with a shared library that is NOT $BASE_VOLUME. `du -x` never
  # crosses a mount point, so measuring /workspace would weigh a few GB of venv against a 500 GB library quota
  # and call a full library nearly empty, which is the one direction a disk gate must never be wrong in.
  if [ -n "${BASE_FAKE_USED_KB:-}" ]; then echo "$BASE_FAKE_USED_KB"; return 0; fi
  local where="${VOL:-$BASE_VOLUME}"
  if [ -n "${BASE_LIBRARY:-}" ] && [ -d "$BASE_LIBRARY" ]; then where="$BASE_LIBRARY"; fi
  du -skx "$where" 2>/dev/null | cut -f1 | grep -E '^[0-9]+$' || echo 0
}
_base_disk_gate(){ # <needed bytes> → 0 proceed, 1 stop. Honest about a pooled filesystem it cannot measure.
  local need="$1" free_b need_b
  if [ -n "${BASE_FAKE_FREE_GB:-}" ]; then free_b="$(_base_gb_to_bytes "$BASE_FAKE_FREE_GB")"; else free_b=$(( $(_base_free_kb "$M") * 1000 )); fi
  need_b=$((need + 10000000000))
  if [ -z "${BASE_FAKE_FREE_GB:-}" ] && _base_fs_is_pool "$M"; then
    local quota used_kb
    quota="$(_base_volume_quota_gb)"
    if [ -n "$quota" ]; then                                   # podctl recorded the volume's size: measure what it holds, judge the real headroom
      used_kb="$(_base_volume_used_kb)"
      free_b=$(( $(_base_gb_to_bytes "$quota") - used_kb * 1000 )); [ "$free_b" -lt 0 ] && free_b=0
      echo "  volume $quota GB (recorded by podctl) · $(_base_bytes_to_gb $((used_kb * 1000))) GB in use · $(_base_bytes_to_gb "$free_b") GB free"
    else
      warn "the volume reports $(_base_bytes_to_gb "$free_b") GB free — that is the backing pool, not your quota, so free space is not verifiable here (podctl upload/ensure records the volume size in state/volume.env)"
      echo "  need $(_base_bytes_to_gb "$need_b") GB (+10 GB headroom included) — proceeding"
      return 0
    fi
  fi
  echo "  need $(_base_bytes_to_gb "$need_b") GB (+10 GB headroom) · $(_base_bytes_to_gb "$free_b") GB free on the volume"
  if [ "$need_b" -gt "$free_b" ]; then
    if [ "$BASE_DRY" = "1" ]; then       # a dry run reports the verdict; it does not fail on it
      warn "the real run would stop here: not enough space on the volume ($(_base_bytes_to_gb "$need_b") GB needed, $(_base_bytes_to_gb "$free_b") GB free)"
      return 0
    fi
    err "not enough space on the volume — nothing downloaded (raise the volume size or reclaim below)"
    BASE_FAILED+=("models: disk gate — $(_base_bytes_to_gb "$need_b") GB needed, $(_base_bytes_to_gb "$free_b") GB free")
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------- stages (3.1.0): the first flow first, the rest behind a started server
# A package may name the files its first flow does NOT need: MODELS_LATER=( <MODELS file> ... ). Only a staged run
# (BASE_STAGED=1, a buyer's machine) acts on it: those rows get a STAND-IN, a 0-byte file at the declared dest, so
# ComfyUI's validator (which checks every active loader's filename, even on branches a run never takes) accepts the
# graph, and a detached `base.sh fetch-later` downloads the real files after ComfyUI has started. A stand-in is
# listed in state/standins.list and nothing counts, moves, renames, prunes or reports it as a model. Every other
# run ignores MODELS_LATER, and treats a stand-in it meets as absent.
_base_later_rows(){ # prints the validated MODELS_LATER entries; exit 1 naming each bad one
  local f cat fam purp file url rest hit bad=0 rows
  rows="$(_base_model_rows 2>/dev/null || true)"
  for f in ${MODELS_LATER[@]+"${MODELS_LATER[@]}"}; do
    hit=""
    while IFS='|' read -r cat fam purp file url rest; do if [ "$file" = "$f" ]; then hit="$url"; break; fi; done <<< "$rows"
    if [ -z "$hit" ]; then echo "  !! MODELS_LATER names '$f', which is no MODELS row's file" >&2; bad=1; continue; fi
    if [ "$hit" = "LOCAL" ]; then echo "  !! MODELS_LATER: '$f' is LOCAL, the buyer's own file: it cannot come later" >&2; bad=1; continue; fi
    case "$f" in */) echo "  !! MODELS_LATER: '$f' is a snapshot folder: a stand-in cannot stand in for a directory" >&2; bad=1; continue;; esac
    echo "$f"
  done
  return $bad
}
_base_is_later(){ local f; for f in ${MODELS_LATER[@]+"${MODELS_LATER[@]}"}; do [ "$f" = "$1" ] && return 0; done; return 1; }
_base_standins(){ echo "$BASE_STATE/standins.list"; }
_base_standin_is(){ # <dest> → 0 when it is a registered stand-in: listed AND still 0 bytes
  [ -f "$1" ] && [ ! -s "$1" ] && [ -f "$(_base_standins)" ] && grep -qxF -- "$1" "$(_base_standins)"
}
_base_standin_make(){ # <dest> → a 0-byte file, registered
  local list; list="$(_base_standins)"; mkdir -p "$(dirname "$1")" "$(dirname "$list")"
  [ -e "$1" ] || : > "$1"
  touch "$list"; grep -qxF -- "$1" "$list" || echo "$1" >> "$list"
}
_base_standin_drop(){ # <dest> → unregistered, and removed while it is still empty
  local list; list="$(_base_standins)"
  if [ -f "$1" ] && [ ! -s "$1" ]; then rm -f "$1"; fi
  if [ -f "$list" ]; then grep -vxF -- "$1" "$list" > "$list.tmp" || true; mv "$list.tmp" "$list"; fi
}
_base_standin_sweep(){ # keep only the listed dests that are still empty (a download replaced the others)
  local list d; list="$(_base_standins)"; [ -f "$list" ] || return 0
  : > "$list.tmp"
  while IFS= read -r d; do if [ -n "$d" ] && [ -f "$d" ] && [ ! -s "$d" ]; then echo "$d" >> "$list.tmp"; fi; done < "$list"
  mv "$list.tmp" "$list"
}
_base_later_queue_add(){ # <spec>... → merged into state/later.queue by dest, so a bundle's packages add up rather than overwrite
  local q="$BASE_STATE/later.queue" spec d; mkdir -p "$BASE_STATE"; touch "$q"
  for spec in "$@"; do
    d="${spec##*|}"
    awk -F'|' -v d="$d" '$5 != d' "$q" > "$q.tmp"; echo "$spec" >> "$q.tmp"; mv "$q.tmp" "$q"
  done
}
_base_progress(){ # [stage] → state/progress.json from the queue, the stand-in list and the disk (a report: never fatal)
  "$SYS_PY" "$BASE_DIR/py/stage_progress.py" --queue "$BASE_STATE/later.queue" --standins "$(_base_standins)" \
    --failed "$BASE_STATE/later.failed" --out "$BASE_STATE/progress.json" --stage "${1:-later}" 2>/dev/null \
    || note "progress.json not written (py/stage_progress.py failed)"
  return 0
}

# ---------------------------------------------------------------- fetching: one at a time, or BASE_FETCH_JOBS at once
_base_fetch_jobs(){ # 3.11.0: six at once by default (one Xet file does not fill a pod's link; about three to six do, measured
  # 2026-10-16: six files 1.7 GB/s together, one 0.8 to 1 GB/s), three on a machine under 32 GiB of memory (each high
  # performance transfer keeps its own buffers). BASE_FETCH_JOBS=1 is the one-at-a-time behaviour of 3.0 to 3.10.
  local n="${BASE_FETCH_JOBS:-}" kb
  if [ -z "$n" ]; then
    kb="$(awk '/^MemTotal:/{print $2}' /proc/meminfo 2>/dev/null || true)"
    if [[ "$kb" =~ ^[0-9]+$ ]] && [ "$kb" -lt 33554432 ]; then n=3; else n=6; fi
  fi
  if [[ "$n" =~ ^[1-9][0-9]*$ ]]; then echo "$n"; else note "BASE_FETCH_JOBS='$n' is not a positive integer: fetching one file at a time" >&2; echo 1; fi
}
_base_fetch_one(){ # <spec KIND|rel|url|bytes|dest> → rc of the download
  local kind rel url bytes dest; IFS='|' read -r kind rel url bytes dest <<<"$1"
  if [ "$kind" = "SNAP" ]; then _base_snapshot "$url" "$dest" "$bytes"; else _base_download "$url" "$dest" "$bytes"; fi
}
_base_fetch_fold(){ # <rc> <secs> <spec> → the verdict line and the run's counters, in the caller's shell
  local rc="$1" secs="$2" kind rel url bytes dest _sz; IFS='|' read -r kind rel url bytes dest <<<"$3"
  if [ "$rc" = "0" ]; then
    if [ "$kind" = "SNAP" ]; then _sz="$bytes"; else _sz="$(_base_fsize "$dest")"; fi
    BASE_DL_BYTES=$(( BASE_DL_BYTES + _sz )); BASE_DL_TIME=$(( BASE_DL_TIME + secs ))
    ok "$rel downloaded ($(_base_bytes_to_gb "$_sz") GB, verified) · $(_base_rate "$_sz" "$secs")"
    MODEL_DL+=("$rel"); DL_DESTS+=("$dest"); MODEL_DL_GB="$(_base_add_gb "$MODEL_DL_GB" "$(_base_bytes_to_gb "$bytes")")"
  else err "download failed: $rel"; MODEL_FAIL+=("$rel"); MODEL_FAIL_GB="$(_base_add_gb "$MODEL_FAIL_GB" "$(_base_bytes_to_gb "$bytes")")"; BASE_FAILED+=("model: $rel not obtained"); fi
}
_base_alive(){ local p c=0; for p in "$@"; do if kill -0 "$p" 2>/dev/null; then c=$((c + 1)); fi; done; echo "$c"; }   # <pid>... → how many still run
_base_fetch_queue(){ # <spec>... → every spec fetched and folded, at most BASE_FETCH_JOBS at a time, reported in queue order
  local n spec i=0 rc t0 logd rel pids=()
  n="$(_base_fetch_jobs)"
  if [ "$n" = "1" ]; then                          # exactly the 3.0 behaviour: live output, one file after another
    for spec in "$@"; do
      IFS='|' read -r _ rel _ <<<"$spec"; echo -e "\n${CYA}→ $rel${NC}"
      BASE_DL_SECS=0; t0=$SECONDS; rc=0; _base_fetch_one "$spec" || rc=$?
      _base_fetch_fold "$rc" "$(( SECONDS - t0 ))" "$spec"
    done
    return 0
  fi
  logd="$(_base_mktemp_d)"
  note "fetching $# file(s), $n at a time, largest first (BASE_FETCH_JOBS)"
  # 3.11.0: started largest first, so the longest transfer is never the one that waits for a slot; reported in row order
  for i in $(i=0; for spec in "$@"; do i=$((i + 1)); IFS='|' read -r _ _ _ b _ <<<"$spec"; echo "${b:-0} $i"; done | sort -k1,1nr -k2,2n | awk '{print $2}'); do
    spec="${!i}"                                   # the i-th argument: queue order is kept for the report below
    # 3.11.0: only this queue's own jobs fill a slot or are waited for; the run has other background work by now
    while [ "$(_base_alive ${pids[@]+"${pids[@]}"})" -ge "$n" ]; do sleep 1; done
    ( t0=$SECONDS; rc=0; _base_fetch_one "$spec" > "$logd/$i.log" 2>&1 || rc=$?; echo "$rc|$(( SECONDS - t0 ))" > "$logd/$i.rc" ) &
    pids+=("$!")
  done
  wait ${pids[@]+"${pids[@]}"} 2>/dev/null || true
  i=0
  for spec in "$@"; do
    i=$((i + 1)); IFS='|' read -r _ rel _ <<<"$spec"; echo -e "\n${CYA}→ $rel${NC}"
    [ -f "$logd/$i.log" ] && cat "$logd/$i.log"
    IFS='|' read -r rc t0 < "$logd/$i.rc" 2>/dev/null || { rc=1; t0=0; }
    _base_fetch_fold "$rc" "$t0" "$spec"
  done
  return 0
}

# ---------------------------------------------------------------- the later stage, detached from the install
_base_later_pending(){ # → the queue's specs whose dest is not yet a complete file
  local q="$BASE_STATE/later.queue" kind rel url bytes dest
  [ -f "$q" ] || return 0
  while IFS='|' read -r kind rel url bytes dest; do
    [ -n "$dest" ] || continue
    if [ -f "$dest" ] && [ "$(_base_fsize "$dest")" = "$bytes" ]; then continue; fi
    echo "$kind|$rel|$url|$bytes|$dest"
  done < "$q"
}
_base_later_launch(){ # after the server started: hand the queue to a detached `base.sh fetch-later`
  local pending n gb=0 b log
  pending="$(_base_later_pending)"; [ -n "$pending" ] || return 0
  n="$(printf '%s\n' "$pending" | awk 'NF' | wc -l | tr -d ' ')"
  while IFS='|' read -r _ _ _ b _; do gb=$((gb + b)); done <<< "$pending"
  LATER_PENDING="$n file(s), $(_base_bytes_to_gb "$gb") GB"
  if [ "$BASE_DRY" = "1" ]; then would "download the later stage ($LATER_PENDING) behind the started server"; return 0; fi
  mkdir -p "$BASE_STATE/logs"; log="$BASE_STATE/logs/fetch-later_$(date +%Y%m%d-%H%M%S).log"
  nohup setsid bash "$BASE_DIR/base.sh" fetch-later </dev/null >> "$log" 2>&1 &
  ok "later stage: $LATER_PENDING downloading in the background (state/progress.json, $log)"
}
base_fetch_later(){ # the verb: fetch every pending later file, pass after pass, until a pass gets nothing new
  base_env_setup
  if [ "${BASE_VOLUME_SHARED:-0}" = "1" ]; then note "fetch-later: a shared store is the operator's shape, where every file is fetched before the server starts; nothing to do"; return 0; fi
  if ! base_discover quiet; then err "fetch-later: no ComfyUI tree found"; return 3; fi
  _base_tmp
  local pidf="$BASE_STATE/fetch-later.pid" other pending pass=0 before after spec need b
  other="$(cat "$pidf" 2>/dev/null || true)"
  if [ -n "$other" ] && [ "$other" != "$$" ] && kill -0 "$other" 2>/dev/null; then note "fetch-later: already running as pid $other; it picks up new queue entries on its next pass"; return 0; fi
  echo "$$" > "$pidf"
  base_tokens >/dev/null 2>&1 < /dev/null || true   # HF_TOKEN from state/tokens.env when the buyer gave one; never a prompt here
  : > "$BASE_STATE/later.failed"
  local -a specs DL_DESTS
  while :; do
    pass=$((pass + 1))
    pending="$(_base_later_pending | grep -vxF -f "$BASE_STATE/later.failed" || true)"
    [ -n "$pending" ] || break
    before="$(printf '%s\n' "$pending" | awk 'NF' | wc -l | tr -d ' ')"
    specs=(); need=0
    while IFS= read -r spec; do if [ -n "$spec" ]; then specs+=("$spec"); IFS='|' read -r _ _ _ b _ <<<"$spec"; need=$((need + b)); fi; done <<< "$pending"
    hdr "LATER STAGE · pass $pass · ${#specs[@]} file(s), $(_base_bytes_to_gb "$need") GB"
    _base_progress later
    if ! _base_disk_gate "$need"; then printf '%s\n' "${specs[@]}" >> "$BASE_STATE/later.failed"; break; fi
    mkdir -p "$STAGING"; [ "$BASE_NO_NET" = "1" ] || _base_xet_check
    DL_DESTS=(); MODEL_FAIL=()
    _base_fetch_queue "${specs[@]}"
    for spec in "${specs[@]}"; do IFS='|' read -r _ b _ <<<"$spec"; if _base_in_list "$b" ${MODEL_FAIL[@]+"${MODEL_FAIL[@]}"}; then echo "$spec" >> "$BASE_STATE/later.failed"; fi; done
    _base_standin_sweep; _base_progress later
    after="$(_base_later_pending | grep -vxF -f "$BASE_STATE/later.failed" | awk 'NF' | wc -l | tr -d ' ')"
    [ "${after:-0}" -lt "$before" ] || break
  done
  _base_standin_sweep; rm -f "$pidf"
  if [ -s "$BASE_STATE/later.failed" ]; then _base_progress later; err "fetch-later: $(wc -l < "$BASE_STATE/later.failed" | tr -d ' ') file(s) not obtained; run 'bash $BASE_DIR/base.sh fetch-later' again"; return 1; fi
  _base_progress done; ok "fetch-later: every later file is in place"; return 0
}

# ---------------------------------------------------------------- the stage
base_models(){
  hdr "MODEL LIBRARY · $M"
  local rows; rows="$(_base_model_rows)" || { err "the MODELS table is invalid (see above)"; BASE_FAILED+=("models: invalid rows"); return 1; }
  _base_build_index
  echo "  indexed $(wc -l < "$IDX" | tr -d ' ') model-shaped files under: $IDX_ROOTS"
  local row cat fam purp file url bytes mnote alts rel dest base sz size path cand cand_dev mdev was_partial
  local -a TODO=() DL_DESTS=() LATER_TODO=()
  local NEED=0 staged_later
  KNOWN=()
  mdev="$(_base_fdev "$M" 2>/dev/null || echo 0)"
  while IFS='|' read -r cat fam purp file url bytes mnote alts; do
    [ -n "$cat" ] || continue
    row="$cat|$fam|$purp|$file|$url|$bytes|$mnote|$alts"
    rel="$(_base_dest_rel "$row")"; dest="$(_base_dest_path "$rel")"; base="$(basename "${file%/}")"
    # shellcheck disable=SC2206
    KNOWN+=("$base" $alts)            # alternates are space-separated basenames: word-splitting is the intent
    # ---- snapshot rows
    if [[ "$file" == */ ]]; then
      local snap_ok=0 sdir="" sdir_c
      if [ -d "$dest" ]; then
        if [ "$url" = "LOCAL" ]; then [ -f "$dest/config.json" ] && snap_ok=1
        elif [ "$(_base_dir_size "$dest")" = "$bytes" ] && ! _base_pin_moved "$dest" "$url"; then snap_ok=1; fi
      fi
      if [ "$snap_ok" = "1" ] && _base_early_got "$dest"; then ok "$rel  (snapshot, downloaded beside the venv build)"
      elif [ "$snap_ok" = "1" ]; then
        sz="$(_base_dir_size "$dest")"; ok "$rel  (snapshot, $(_base_bytes_to_gb "$sz") GB)"; MODEL_OK+=("$rel"); MODEL_OK_GB="$(_base_add_gb "$MODEL_OK_GB" "$(_base_bytes_to_gb "$sz")")"
      elif [ "$url" = "LOCAL" ]; then
        # a LOCAL snapshot never downloads: a folder of that name holding a config.json, anywhere on the pod, is moved into place
        while IFS= read -r sdir_c; do
          if [ -n "$sdir_c" ] && [ "$sdir_c" != "$dest" ] && [ -f "$sdir_c/config.json" ]; then case "$sdir_c" in "$STAGING_ROOT"/*) ;; *) sdir="$sdir_c"; break;; esac; fi
        done < <(_base_find_dirs "$base")
        if [ -n "$sdir" ]; then
          if [ "$BASE_DRY" = "1" ]; then would "move the snapshot folder $sdir → $rel"; MODEL_MOVED+=("$rel (would move from $sdir)")
          elif _base_move_dir_into_place "$sdir" "$dest"; then ok "$rel ← moved from $sdir"; MODEL_MOVED+=("$rel ← $sdir"); MODEL_MOVED_GB="$(_base_add_gb "$MODEL_MOVED_GB" "$(_base_bytes_to_gb "$(_base_dir_size "$dest")")")"
          else err "move failed: $sdir → $dest"; MODEL_FAIL+=("$rel (move failed)"); BASE_FAILED+=("snapshot move failed: $rel"); fi
        elif [ "$BASE_DRY" = "1" ] && declare -F pkg_pre_models >/dev/null; then
          would "find or be given the snapshot folder for $rel (LOCAL, and this package's pkg_pre_models may place it before the list is walked, so --check cannot judge it)"
        else
          err "$rel is declared LOCAL and no folder '$base' holding a config.json exists anywhere on this pod — place it on the volume and re-run"
          MODEL_LOCAL_MISSING+=("$rel"); MODEL_FAIL+=("$rel (LOCAL, not found)"); BASE_FAILED+=("LOCAL snapshot not found on this pod: $base → $rel")
        fi
      elif [ "$BASE_DRY" = "1" ]; then would "download snapshot $url → $rel ($(_base_bytes_to_gb "$bytes") GB)"; TODO+=("SNAP|$rel|$url|$bytes|$dest"); NEED=$((NEED + bytes))
      else TODO+=("SNAP|$rel|$url|$bytes|$dest"); NEED=$((NEED + bytes)); todo "$rel  snapshot (~$(_base_bytes_to_gb "$bytes") GB) — queued"; fi
      continue
    fi
    was_partial=0; staged_later=0
    if [ "${BASE_STAGED:-0}" = "1" ] && _base_is_later "$file"; then staged_later=1; fi
    if _base_standin_is "$dest"; then                # a stand-in is never a partial file: never renamed, never counted
      if [ "$staged_later" = "0" ] && [ "$BASE_DRY" != "1" ]; then _base_standin_drop "$dest"; fi   # not staged here: simply absent
    elif [ -f "$dest" ]; then
      sz="$(_base_fsize "$dest")"
      if _base_size_ok "$sz" "$bytes" && _base_early_got "$dest"; then ok "$rel  ($(_base_bytes_to_gb "$sz") GB, downloaded beside the venv build)"; continue; fi   # counted once, as downloaded
      if _base_size_ok "$sz" "$bytes" && ! _base_pin_moved "$dest" "$url"; then ok "$rel  ($(_base_bytes_to_gb "$sz") GB)"; MODEL_OK+=("$rel"); MODEL_OK_GB="$(_base_add_gb "$MODEL_OK_GB" "$(_base_bytes_to_gb "$sz")")"; continue; fi
      if _base_size_ok "$sz" "$bytes"; then miss "$rel: its row now pins a commit whose file differs from this one — fetched again"
      else miss "$rel is $(_base_bytes_to_gb "$sz") GB, expected $(_base_bytes_to_gb "$bytes") GB — partial"; fi
      if [ "$BASE_DRY" = "1" ]; then would "rename to $base.partial and re-fetch"
      else mv "$dest" "$dest.partial"; DEL_FILES+=("$dest.partial"); DEL_BYTES=$((DEL_BYTES + sz)); fi
      MODEL_PARTIAL+=("$rel"); was_partial=1
    fi
    # ---- an indexed file with the exact basename (or a listed alternate) AND the expected size → moved into the library
    cand=""; cand_dev=""
    # shellcheck disable=SC2086
    while IFS=$'\t' read -r size path; do
      if [ -z "$path" ] || [ ! -f "$path" ] || [ "$path" = "$dest" ]; then continue; fi
      case "$path" in "$STAGING_ROOT"/*|*.partial) continue;; esac
      if _base_size_ok "$size" "$bytes"; then
        if [ -z "$cand" ] || { [ "$cand_dev" != "$mdev" ] && [ "$(_base_fdev "$path")" = "$mdev" ]; }; then cand="$path"; cand_dev="$(_base_fdev "$path")"; fi
      else note "$base: same name, different size, left alone: $path ($(_base_bytes_to_gb "$size") GB)"; fi
    done < <(_base_idx_find "$base" $alts)
    if [ -n "$cand" ]; then
      if [ "$BASE_DRY" = "1" ]; then would "move $cand → $rel"; MODEL_MOVED+=("$rel (would move from $cand)")
      elif _base_move_into_place "$cand" "$dest"; then ok "$rel ← moved from $cand"; MODEL_MOVED+=("$rel ← $cand"); MODEL_MOVED_GB="$(_base_add_gb "$MODEL_MOVED_GB" "$(_base_bytes_to_gb "$(_base_fsize "$dest")")")"
      else err "move failed: $cand → $dest"; TODO+=("FILE|$rel|$url|$bytes|$dest"); NEED=$((NEED + bytes)); fi
      continue
    fi
    # ---- LOCAL rows never download: not found is a failure, by name.
    # UNLESS this is a dry run AND the package declares pkg_pre_models. A LOCAL row is placed by that hook, which
    # does nothing under BASE_DRY, so at --check time the file is absent BY DESIGN and its absence carries no
    # information: `podctl install --pkg` was gated on a condition the gate itself created and every package with
    # a LOCAL row stopped before it could place the file. Same shape as _base_disk_gate, which reports free space
    # it cannot measure. A package with NO such hook is still gated, because there a missing file really is
    # missing and saying so before the run is the useful answer.
    if [ "$url" = "LOCAL" ] && [ "$BASE_DRY" = "1" ] && declare -F pkg_pre_models >/dev/null; then
      would "find or be given $rel (LOCAL, and this package's pkg_pre_models may place it before the list is walked, so --check cannot judge it)"
      continue
    fi
    if [ "$url" = "LOCAL" ]; then
      err "$rel is declared LOCAL and was not found anywhere on this pod — place $base on the volume and re-run"
      MODEL_LOCAL_MISSING+=("$rel"); MODEL_FAIL+=("$rel (LOCAL, not found)")
      BASE_FAILED+=("LOCAL model not found on this pod: $base → $rel")
      continue
    fi
    if [ "$staged_later" = "1" ]; then              # the first flow does not need it: a stand-in now, the file behind the started server
      if [ "$BASE_DRY" = "1" ]; then would "stand in for $rel (~$(_base_bytes_to_gb "$bytes") GB), fetched after ComfyUI starts"
      else _base_standin_make "$dest"; todo "$rel  (~$(_base_bytes_to_gb "$bytes") GB): later stage, a stand-in until it arrives"; fi
      LATER_TODO+=("FILE|$rel|$url|$bytes|$dest"); MODEL_STANDIN+=("$rel")
      continue
    fi
    if [ "$was_partial" = "1" ]; then echo "      re-queued for download"
    else todo "$rel  (~$(_base_bytes_to_gb "$bytes") GB) — not on disk anywhere, queued for download"; [ -n "$mnote" ] && echo "      $mnote" || true; fi
    TODO+=("FILE|$rel|$url|$bytes|$dest"); NEED=$((NEED + bytes))
  done <<< "$rows"

  _base_early_settle                     # 3.11.0: what the early pass failed is failed here, by name, not fetched a second time
  # ---- downloads, behind the gate
  if [ "${#TODO[@]}" -gt 0 ]; then
    hdr "DOWNLOADS · ${#TODO[@]} item(s), $(_base_bytes_to_gb "$NEED") GB needed"
    local kind spec
    if [ "$BASE_DRY" = "1" ]; then
      _base_disk_gate "$NEED" || true
      for spec in "${TODO[@]}"; do IFS='|' read -r kind rel url bytes dest <<<"$spec"; would "download $(_base_bytes_to_gb "$bytes") GB → $rel"; done
    elif ! _base_disk_gate "$NEED"; then
      for spec in "${TODO[@]}"; do IFS='|' read -r kind rel url bytes dest <<<"$spec"; MODEL_FAIL+=("$rel (no disk)"); MODEL_FAIL_GB="$(_base_add_gb "$MODEL_FAIL_GB" "$(_base_bytes_to_gb "$bytes")")"; done
    else
      mkdir -p "$STAGING"
      [ "$BASE_NO_NET" = "1" ] || _base_xet_check
      _base_fetch_queue "${TODO[@]}"
      rmdir "$STAGING" 2>/dev/null || true
      [ "$STAGING" = "$STAGING_ROOT" ] || rmdir "$STAGING_ROOT" 2>/dev/null || true
      # a *.partial this run renamed is dropped once its complete copy is verified in place
      local keep=() kept_bytes=0 f
      for f in ${DEL_FILES[@]+"${DEL_FILES[@]}"}; do
        if [[ "$f" == *.partial ]] && [ -f "${f%.partial}" ] && _base_in_list "${f%.partial}" ${DL_DESTS[@]+"${DL_DESTS[@]}"}; then rm -f "$f"; note "removed $(basename "$f") (re-fetched and verified)"
        else keep+=("$f"); kept_bytes=$((kept_bytes + $(_base_fsize "$f" 2>/dev/null || echo 0))); fi
      done
      DEL_FILES=(${keep[@]+"${keep[@]}"}); DEL_BYTES=$kept_bytes
    fi
  fi
  if [ "${#LATER_TODO[@]}" -gt 0 ] && [ "$BASE_DRY" != "1" ]; then _base_later_queue_add "${LATER_TODO[@]}"; fi
  if [ "${BASE_STAGED:-0}" = "1" ] && [ "$BASE_DRY" != "1" ]; then _base_progress first; fi
  echo "  downloaded ${#MODEL_DL[@]} · moved ${#MODEL_MOVED[@]} · in place ${#MODEL_OK[@]} · missing ${#MODEL_FAIL[@]}${LATER_TODO[0]+ · later ${#LATER_TODO[@]} (stand-ins until they arrive)}"
  if [ "${#MODEL_FAIL[@]}" -gt 0 ]; then err "${#MODEL_FAIL[@]} required file(s) missing — the run will exit non-zero at the summary"; fi
  return 0
}

# ---------------------------------------------------------------- one deletion prompt, scoped by the ledger
_base_family_dirs(){ # → the library dirs this package's rows write into (category/Family, or category/Purpose for freeform), plus LEGACY_DIRS
  local rows cat fam purp rest d seen=" "
  rows="$(_base_model_rows 2>/dev/null || true)"
  while IFS='|' read -r cat fam purp rest; do
    [ -n "$cat" ] || continue
    d="$cat"; if [ -n "$fam" ]; then d="$d/$fam"; elif [ -n "$purp" ]; then d="$d/$purp"; fi
    case "$seen" in *" $d "*) ;; *) seen="$seen$d "; echo "$d";; esac
  done <<< "$rows"
  for d in ${LEGACY_DIRS[@]+"${LEGACY_DIRS[@]}"}; do case "$seen" in *" $d "*) ;; *) seen="$seen$d "; echo "$d";; esac; done
}
# ---------------------------------------------------------------- the deletion, staged and reversible (2.5.12)
# Every guard before this point is an INFERENCE about identity: is this path the same file as that one. 2.5.11
# fixed the inference that was wrong (path strings, where dev:ino was needed) and 63.52 GB of live models had
# already been offered before anyone noticed. An inference can be wrong again in a shape nobody has met, and the
# consequence is a model that no longer exists anywhere, on a store several machines mount, with the ledger still
# recording it installed and base_models already past.
#
# So the delete stops being a one-way step. Each candidate is RENAMED ASIDE in its own directory, which is atomic
# and never crosses a filesystem; every destination the package declares is then re-stat'd; and only when all of
# them are still present at their declared size is anything actually removed. If any destination vanished or
# changed size, every rename is undone and the run says so. It needs to know nothing about mounts, inodes or
# symlinks to be safe: it checks the thing that actually matters, which is whether the models are still there.
_base_prune_commit(){ # <rows> — stage, verify, then commit or roll back
  local rows="$1" f staged=() sdirs=() bad="" row cat fam purp file url bytes mnote alts rel dest sz
  # ---- stage: rename aside, atomically, beside the original
  for f in ${DEL_FILES[@]+"${DEL_FILES[@]}"}; do
    if [ -f "$f" ] && [ ! -L "$f" ]; then
      if mv -- "$f" "$f$BASE_PRUNE_SUFFIX" 2>/dev/null; then staged+=("$f"); else warn "could not set aside $f — left alone"; fi
    fi
  done
  for f in ${DEL_DIRS[@]+"${DEL_DIRS[@]}"}; do
    if [ -d "$f" ]; then
      if mv -- "$f" "$f$BASE_PRUNE_SUFFIX" 2>/dev/null; then sdirs+=("$f"); else warn "could not set aside $f/ — left alone"; fi
    fi
  done
  # ---- verify: every declared destination still present, at its declared size
  while IFS='|' read -r cat fam purp file url bytes mnote alts; do
    [ -n "$cat" ] || continue
    [[ "$file" == */ ]] && continue
    rel="$(_base_dest_rel "$cat|$fam|$purp|$file|$url|$bytes|$mnote|$alts")"; dest="$(_base_dest_path "$rel")"
    _base_standin_is "$dest" && continue              # a stand-in is a promise, not a model: nothing to re-check yet
    if [ ! -f "$dest" ]; then bad="$rel is GONE"; break; fi
    sz="$(_base_fsize "$dest")"
    if [ -n "$bytes" ] && [ "$bytes" != "0" ] && [ "$sz" != "$bytes" ]; then bad="$rel changed size ($sz, expected $bytes)"; break; fi
  done <<< "$rows"
  # ---- commit, or put everything back
  if [ -n "$bad" ]; then
    for f in ${staged[@]+"${staged[@]}"}; do mv -- "$f$BASE_PRUNE_SUFFIX" "$f" 2>/dev/null || true; done
    for f in ${sdirs[@]+"${sdirs[@]}"}; do mv -- "$f$BASE_PRUNE_SUFFIX" "$f" 2>/dev/null || true; done
    err "deletion ROLLED BACK, nothing was removed: $bad"
    err "  a file this sweep called surplus was the only copy of a model the package declares."
    BASE_FAILED+=("prune rolled back: $bad")
    return 1
  fi
  for f in ${staged[@]+"${staged[@]}"}; do rm -f -- "$f$BASE_PRUNE_SUFFIX" && echo "  ✂ deleted $f"; done
  for f in ${sdirs[@]+"${sdirs[@]}"}; do rm -rf -- "$f$BASE_PRUNE_SUFFIX" && echo "  ✂ deleted $f/"; done
  ok "$(_base_bytes_to_gb "$DEL_BYTES") GB reclaimed (every declared model re-checked first)"
  DEL_FILES=(); DEL_DIRS=()
  return 0
}

base_prune(){ # legacy leftovers, duplicates, superseded, partials → ONE y/N (default No); a file another package claims is never offered
  hdr "OLD LAYOUT · DUPLICATES · SUPERSEDED"
  if [ "${BASE_STAGED:-0}" = "1" ]; then note "prune: skipped, a staged install (later files are still arriving, and a buyer's fresh disk holds nothing to reclaim)"; return 0; fi
  [ -n "$IDX" ] && [ -f "$IDX" ] || _base_build_index
  local rows cat fam purp file url bytes mnote alts rel dest base real dsz size path legacy f pat hit lbase d
  rows="$(_base_model_rows 2>/dev/null || true)"
  # ---- legacy folders: known files were handled by base_models; the rest is unknown and stays
  for legacy in ${LEGACY_DIRS[@]+"${LEGACY_DIRS[@]}"}; do
    [ -d "$M/$legacy" ] || continue
    while IFS= read -r f; do
      [ -n "$f" ] || continue; base="$(basename "$f")"
      if _base_in_list "$base" ${KNOWN[@]+"${KNOWN[@]}"}; then continue; fi
      hit=0; for pat in ${SUPERSEDED[@]+"${SUPERSEDED[@]}"}; do lbase="$(_base_lower "$base")"; if [[ "$lbase" == $(_base_lower "$pat") ]]; then hit=1; break; fi; done
      if [ "$hit" = "1" ]; then continue; fi
      if _base_in_list "${f#"$M"/}" ${UNKNOWN_FILES[@]+"${UNKNOWN_FILES[@]}"}; then continue; fi
      UNKNOWN_FILES+=("${f#"$M"/}"); note "unknown, left alone: ${f#"$M"/}"
    done < <(find "$M/$legacy" -maxdepth 2 -type f 2>/dev/null)
  done
  # ---- unclaimed: model files in the library's flat category folders that no row declares (an image's own downloads,
  #      the user's files). Reported, never moved or deleted; a later package that declares one claims it.
  local catdir e first=1 nexpr=()
  for e in $BASE_MODEL_EXTS; do case "$e" in '*.partial') continue;; esac; if [ "$first" = 1 ]; then nexpr=( -name "$e" ); first=0; else nexpr+=( -o -name "$e" ); fi; done
  for catdir in "$M"/*/; do
    [ -d "$catdir" ] || continue
    while IFS= read -r f; do
      [ -n "$f" ] || continue; base="$(basename "$f")"
      if _base_in_list "$base" ${KNOWN[@]+"${KNOWN[@]}"}; then continue; fi
      hit=0; for pat in ${SUPERSEDED[@]+"${SUPERSEDED[@]}"}; do lbase="$(_base_lower "$base")"; if [[ "$lbase" == $(_base_lower "$pat") ]]; then hit=1; break; fi; done
      if [ "$hit" = "1" ]; then continue; fi
      if _base_in_list "${f#"$M"/}" ${UNKNOWN_FILES[@]+"${UNKNOWN_FILES[@]}"} ${UNCLAIMED_FILES[@]+"${UNCLAIMED_FILES[@]}"}; then continue; fi
      UNCLAIMED_FILES+=("${f#"$M"/}")
    done < <(find "$catdir" -maxdepth 1 -type f \( "${nexpr[@]}" \) 2>/dev/null)
  done
  if [ "${#UNCLAIMED_FILES[@]}" -gt 0 ]; then note "unclaimed ${#UNCLAIMED_FILES[@]} model file(s) no package declares — left where they are: ${UNCLAIMED_FILES[*]}"; fi
  # ---- duplicates: same basename (or alternate) + same size as the copy in the library, not claimed by another package
  #
  # NOT on a shared store. "Reclaimable" is not a property one machine can determine there: several machines
  # mount the same root, this run can see none of their ledgers, and a file this machine calls surplus may be
  # the only copy another is mid-render on. MEASURED 2026-09-19 on a three-machine store: the sweep offered
  # 63.52 GB of LIVE models for deletion (Krea 2 RAW, Turbo, the text encoder, the VAE and an adapter), because
  # the store was mounted at two points and each file was therefore indexed under two different paths.
  if [ "$BASE_VOLUME_SHARED" = "1" ]; then
    note "shared store: the duplicate sweep is skipped (other machines mount this root and their copies are not this run's to judge)"
  else
  while IFS='|' read -r cat fam purp file url bytes mnote alts; do
    [ -n "$cat" ] || continue
    [[ "$file" == */ ]] && continue
    rel="$(_base_dest_rel "$cat|$fam|$purp|$file|$url|$bytes|$mnote|$alts")"; dest="$(_base_dest_path "$rel")"; [ -f "$dest" ] || continue
    real="$(cd "$(dirname "$dest")" && pwd -P)/$(basename "$dest")"; dsz="$(_base_fsize "$dest")"
    # shellcheck disable=SC2086
    while IFS=$'\t' read -r size path; do
      if [ -z "$path" ] || [ ! -f "$path" ] || [ -L "$path" ]; then continue; fi
      case "$path" in *.partial) continue;; esac
      if [ "$(cd "$(dirname "$path")" && pwd -P)/$(basename "$path")" = "$real" ]; then continue; fi
      # The same file reached by another route is not a duplicate of itself. `-ef` compares dev:ino, which
      # catches a hardlink and a second mountpoint alike; the path comparison above catches only symlinks.
      # Deleting either name of one inode reclaims NOTHING, so such a path must never reach DEL_FILES.
      if [ "$path" -ef "$dest" ]; then continue; fi
      if [ "$size" != "$dsz" ]; then note "same name, different size (kept): $path ($(_base_bytes_to_gb "$size") GB vs $(_base_bytes_to_gb "$dsz") GB in the library)"; continue; fi
      case "$path" in "$M"/*) if _base_ledger_claims "${path#"$M"/}"; then note "duplicate of $rel at $path is claimed by another installed package — kept"; continue; fi;; esac
      # Two candidates that share an inode with EACH OTHER (rather than with dest) would both be listed and
      # both counted, so the prompt would overstate what is reclaimable even though the second unlink is a
      # no-op. Count a given inode once.
      _dup_seen=0
      for _d in ${DEL_FILES[@]+"${DEL_FILES[@]}"}; do
        if [ "$path" -ef "$_d" ]; then _dup_seen=1; break; fi
      done
      [ "$_dup_seen" = 1 ] && continue
      echo -e "${YEL}  ≡ duplicate of $rel: $path ($(_base_bytes_to_gb "$size") GB)${NC}"; DEL_FILES+=("$path"); DEL_BYTES=$((DEL_BYTES + size))
    done < <(_base_idx_find "$(basename "$file")" $alts)
  done <<< "$rows"
  fi
  # ---- superseded: only inside this package's own family folders (and its declared legacy dirs), only by basename
  local fdirs; fdirs="$(_base_family_dirs)"
  # A pattern the sweep below can never match is skipped by design (basenames only, ever: a path pattern would
  # widen the sweep across a store several machines share, and a trained LoRA is an output, not a row). The
  # SILENCE was the defect: a package declaring such a row got no signal that its entry did nothing. Hoisted out
  # of the per-file loop so it says this once per pattern, not once per indexed file.
  local _sp
  for _sp in ${SUPERSEDED[@]+"${SUPERSEDED[@]}"}; do
    case "$_sp" in
      */*) warn "SUPERSEDED entry ignored, it is a path and this sweep matches basenames only: $_sp";;
      .*)  warn "SUPERSEDED entry ignored, it starts with a dot: $_sp";;
      "")  ;;
    esac
  done
  while IFS=$'\t' read -r size path; do
    if [ -z "$path" ] || [ ! -f "$path" ] || [ -L "$path" ]; then continue; fi
    base="$(basename "$path")"; lbase="$(_base_lower "$base")"; hit=0
    case "$path" in "$M"/*.partial|"$M"/*/*.partial|"$M"/*/*/*.partial|"$M"/*/*/*/*.partial) hit=1;; esac
    if [ "$hit" = "0" ]; then
      local inside=0
      while IFS= read -r d; do [ -n "$d" ] || continue; case "$path/" in "$M/$d"/*) inside=1;; esac; done <<< "$fdirs"
      [ "$inside" = "1" ] || continue
      for pat in ${SUPERSEDED[@]+"${SUPERSEDED[@]}"}; do
        case "$pat" in */*|.*|"") continue;; esac                       # basenames only, ever
        if [[ "$lbase" == $(_base_lower "$pat") ]]; then hit=1; break; fi
      done
    fi
    [ "$hit" = "1" ] || continue
    if _base_in_list "$path" ${DEL_FILES[@]+"${DEL_FILES[@]}"}; then continue; fi
    if _base_ledger_claims "${path#"$M"/}"; then note "superseded here but claimed by another installed package — kept: $path"; continue; fi
    echo -e "${YEL}  ✂ superseded: $path ($(_base_bytes_to_gb "$size") GB)${NC}"; DEL_FILES+=("$path"); DEL_BYTES=$((DEL_BYTES + size))
  done < "$IDX"
  # ---- the single deletion prompt — default No
  if [ "${#DEL_FILES[@]}" -eq 0 ] && [ "${#DEL_DIRS[@]}" -eq 0 ]; then ok "no duplicates, partials or superseded files"
  elif [ "$BASE_DRY" = "1" ]; then note "${#DEL_FILES[@]} file(s) + ${#DEL_DIRS[@]} dir(s), $(_base_bytes_to_gb "$DEL_BYTES") GB reclaimable — the real run offers them for deletion (y/N)"
  else
    echo
    if _base_confirm "Delete these ${#DEL_FILES[@]} files and ${#DEL_DIRS[@]} directories ($(_base_bytes_to_gb "$DEL_BYTES") GB reclaimable)? [y/N] "; then
      _base_prune_commit "$rows"
    else note "kept — nothing deleted"; fi
  fi
  if [ "$BASE_DRY" != "1" ]; then
    for legacy in ${LEGACY_DIRS[@]+"${LEGACY_DIRS[@]}"}; do
      if [ -d "$M/$legacy" ] && [ -z "$(ls -A "$M/$legacy" 2>/dev/null)" ]; then rmdir "$M/$legacy" 2>/dev/null && note "removed empty legacy folder $legacy"; fi
    done
  fi
  return 0
}

# ---------------------------------------------------------------- gen-models: fill/verify bytes from the Hub
_base_hub_sha256(){ # <url> → the file's sha256 as the Hub publishes it (x-linked-etag on an LFS file), or nothing
  local url="$1" out e
  [[ "$url" == https://huggingface.co/* ]] || return 0                 # the Hub is the one host that states it on a HEAD
  _base_hub_auth "$url"
  out="$(curl -sIL --max-time 40 --retry 2 ${BASE_HUB_AUTH[@]+"${BASE_HUB_AUTH[@]}"} "$url" 2>/dev/null | tr -d '\r' || true)"
  e="$(printf '%s\n' "$out" | awk 'tolower($1)=="x-linked-etag:"{v=$2} END{print v}' | tr -d '"' | tr 'A-F' 'a-f')"
  if [[ "$e" =~ ^[0-9a-f]{64}$ ]]; then echo "$e"; fi
  return 0
}
base_gen_hashes(){ # <pkg dir> [--check] → models.sha256 beside the script: each Hub file's sha256, taken from the Hub (3.4.0)
  # Nothing is downloaded or hashed here. --check writes nothing and exits 1 when the file is missing or differs.
  local d="${1%/}" check="${2:-}" s rows cat fam purp file url bytes note alts h tmp rc=0
  _base_tmp                                                             # here, not inside the $( ) below: a temp dir made in a subshell is never cleaned
  s="$(ls "$d"/*"-script.sh" 2>/dev/null | head -1 || true)"
  if [ -z "$s" ]; then echo "  !! no '*-script.sh' in $d" >&2; return 1; fi
  rows="$(BASE_DECLARE_ONLY=1 bash "$s" | grep '^MODELROW ' | cut -c10- || true)"
  tmp="$(mktemp)"
  while IFS='|' read -r cat fam purp file url bytes note alts; do
    [ -n "$cat" ] || continue
    case "$url" in LOCAL|hf://*) continue;; esac                        # no file URL: a local file, or a repo snapshot
    case "$file" in */) continue;; esac
    h="$(_base_hub_sha256 "$url")"
    if [ -n "$h" ]; then printf '%s  %s\n' "$h" "$file" >> "$tmp"
    else echo "  ○ $file: the host states no sha256 for $url (only the Hub does): left out" >&2; fi
  done <<< "$rows"
  sort -k2 -o "$tmp" "$tmp"
  if [ "$check" = "--check" ]; then
    if ! cmp -s "$tmp" "$d/models.sha256"; then
      echo "  !! $d/models.sha256 is not what the Hub says:" >&2; diff "$d/models.sha256" "$tmp" >&2 || true; rc=1
    else echo "  models.sha256 matches the Hub ($(grep -c . "$tmp" | tr -d ' ') file(s))"; fi
    rm -f "$tmp"; return $rc
  fi
  mv "$tmp" "$d/models.sha256"; echo "  wrote $d/models.sha256 ($(grep -c . "$d/models.sha256" | tr -d ' ') file(s))"
  return 0
}
_base_model_hashes(){ # → "file|sha256" for each line of models.sha256 beside the script; exit 1 naming a malformed line
  local f="${PKG_DIR:-.}/models.sha256" h file bad=0
  [ -f "$f" ] || return 0
  while read -r h file; do
    [ -n "$h" ] || continue
    if [[ "$h" =~ ^[0-9a-f]{64}$ ]] && [ -n "$file" ]; then echo "$file|$h"
    else echo "  !! models.sha256: not '<sha256>  <file>': $h $file" >&2; bad=1; fi
  done < "$f"
  return $bad
}
_base_hub_commit(){ # <url> → the 40-hex commit a Hub URL's revision is at now (resolve: the x-repo-commit of the first hop;
  # hf://: the API's sha), or nothing
  local url="$1" ref repo rev c=""
  ref="$(_base_hf_ref "$url")" || return 0
  IFS='|' read -r repo rev _ <<< "$ref"
  case "$url" in
    hf://*) _base_hub_auth "https://huggingface.co/"
            c="$(curl -s --max-time 40 --retry 2 ${BASE_HUB_AUTH[@]+"${BASE_HUB_AUTH[@]}"} "https://huggingface.co/api/models/$repo/revision/${rev:-main}" 2>/dev/null \
                 | "$SYS_PY" -c 'import json,sys; print(json.load(sys.stdin).get("sha",""))' 2>/dev/null || true)";;
    *)      _base_hub_auth "$url"
            c="$(curl -sI --max-time 40 --retry 2 ${BASE_HUB_AUTH[@]+"${BASE_HUB_AUTH[@]}"} "$url" 2>/dev/null | tr -d '\r' \
                 | awk 'tolower($1)=="x-repo-commit:"{print $2; exit}' || true)";;
  esac
  if [[ "$c" =~ ^[0-9a-f]{40}$ ]]; then echo "$c"; fi
  return 0
}
_base_pin_url(){ # <url> → the same URL pinned to the commit its revision is at now; unchanged when already pinned; exit 1 when unreadable
  local url="$1" repo rev path c
  IFS='|' read -r repo rev path <<< "$(_base_hf_ref "$url")"
  if [[ "$rev" =~ ^[0-9a-f]{40}$ ]]; then echo "$url"; return 0; fi
  c="$(_base_hub_commit "$url")"
  if [ -z "$c" ]; then echo "$url"; return 1; fi
  case "$url" in hf://*) echo "hf://$repo@$c";; *) echo "https://huggingface.co/$repo/resolve/$c/$path";; esac
}
base_gen_models(){ # <pkg dir> [--pin] → the package's MODELS rows with bytes filled from HEAD; exit 1 on drift or an unreadable size
  # --pin (3.12.0): every Hub row comes back pinned to the commit its revision is at now (resolve/<commit>/…, hf://…@<commit>)
  local d="${1%/}" pin="${2:-}" s rows rc=0 cat fam purp file url bytes note alts exp pinned
  if [ -n "$pin" ] && [ "$pin" != "--pin" ]; then echo "  !! gen-models <dir> [--pin], not '$pin'" >&2; return 2; fi
  _base_tmp                                                             # here, not inside the $( ) below: a temp dir made in a subshell is never cleaned
  s="$(ls "$d"/*"-script.sh" 2>/dev/null | head -1 || true)"
  if [ -z "$s" ]; then echo "  !! no '*-script.sh' in $d" >&2; return 1; fi
  rows="$(BASE_DECLARE_ONLY=1 bash "$s" | grep '^MODELROW ' | cut -c10- || true)"
  while IFS='|' read -r cat fam purp file url bytes note alts; do
    [ -n "$cat" ] || continue
    if [ "$pin" = "--pin" ] && _base_hf_ref "$url" >/dev/null; then
      if pinned="$(_base_pin_url "$url")"; then url="$pinned"; else echo "  !! $file: the Hub named no commit for $url (left unpinned)" >&2; rc=1; fi
    fi
    case "$url" in
      LOCAL|hf://*) echo "$cat|$fam|$purp|$file|$url|$bytes|$note|$alts";;
      *) exp="$(_base_expected "$url")"
         if [ "${exp%%:*}" = "exact" ]; then
           if [ "$bytes" != "${exp#*:}" ]; then echo "  !! $file: declared $bytes bytes, the Hub says ${exp#*:}" >&2; rc=1; fi
           echo "$cat|$fam|$purp|$file|$url|${exp#*:}|$note|$alts"
         else echo "  !! $file: the Hub returned no size for $url" >&2; rc=1; echo "$cat|$fam|$purp|$file|$url|$bytes|$note|$alts"; fi;;
    esac
  done <<< "$rows"
  return $rc
}
