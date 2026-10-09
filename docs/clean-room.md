# Clean-Room Process

Pitlane is built as a clean-room implementation. The **specification team** writes behavior and interface documents (this repository). The **implementation team** writes code from those documents alone. Keeping the teams apart means the code can be shown to derive only from the spec and from public standards, and not from any existing product's source.

## 1. Roles

| Role | May read | Produces | Must not |
|---|---|---|---|
| **Spec author** | Anything legally accessible, including observing third-party products as a user | `SPEC.md`, `docs/*` | Commit code. Paste third-party code, minified bundles, or deobfuscated logic into the spec. |
| **Implementer** | This repository and the permitted references (§3) | Source code, tests | Read third-party source or bundles of products in the same space. Inspect a competitor's network traffic. Ask spec authors "how does X do it". |
| **Clean-room monitor** | Everything | Approval of each spec change and each Q&A answer | Write spec or code |

For a one- or two-person project, the same person may not hold both the author and implementer roles for the same component. If that isn't possible, record it in the contamination log (§4) and rely on the spec-only rule for code.

## 2. Information flow

```
 Spec authors ──(spec PRs)──▶ Monitor review ──▶ main branch ──▶ Implementers
      ▲                                                             │
      └──────────── written questions (docs/qa/NNN.md) ◀────────────┘
```

- Questions from implementers go in writing to `docs/qa/NNN-short-title.md`. The spec author answers in the same file. The monitor checks that the answer describes *behavior*, not another product's implementation, before it merges.
- Every answer that changes behavior MUST also update `SPEC.md` or the relevant doc. The Q&A file is the audit trail, not the source of truth.
- Verbal or chat answers do not count. If it is not written down, it did not happen.

## 3. Permitted references for implementers

Implementers may use public standards and official vendor documentation:

| Topic | Reference |
|---|---|
| HLS | RFC 8216 |
| MPEG-TS | ISO/IEC 13818-1 (and `ffmpeg` docs) |
| XMLTV | The XMLTV DTD and format wiki |
| HDHomeRun HTTP API | SiliconDust's published developer documentation |
| Plex | Official Plex support articles (Live TV & DVR, library naming, XMLTV) and the documented PMS HTTP API |
| F1 calendar | Jolpica F1 API documentation |
| Libraries | Each dependency's own docs and source (they are dependencies, not competitors) |

General-purpose open-source projects that solve adjacent problems (M3U-to-tuner proxies such as Threadfin, xTeVe, Telly, or Plex DVR scripts) are **not** permitted reading for implementers. If an implementer has already read one, they record it in the contamination log and do not implement the overlapping component.

## 4. Records

- `docs/contamination-log.md` lists who has seen which third-party materials and when, and which components they therefore must not implement.
- Every commit is signed off (`git commit -s`) as a declaration that it was written from this spec and the permitted references.
- Spec changes merge only with monitor approval. Keep the review on the PR.

## 5. Scope rules (content)

Clean room governs **how the code is written**. It does not make any content source lawful. The following rules apply to the specification and the code:

1. The spec and the codebase MUST NOT name, link, or contain adapters, URL patterns, tokens, or reverse-engineered APIs for any specific third-party streaming or replay site.
2. Built-in providers are generic formats only (M3U, channel JSON, directory, HDHomeRun). Sample configs use `example.invalid` URLs and locally generated test streams.
3. Pitlane MUST NOT include features whose only purpose is evading a source's access controls: DRM removal, token forging, referer or origin spoofing aimed at a particular site, CAPTCHA or bot-challenge solving.
4. Operator docs state that the operator is responsible for having the rights to every source they configure.

Plain HTTP header configuration (e.g. a user agent) is allowed as a generic feature, because legitimate IPTV providers often require it.

## 6. Spec-writing guidelines

- Describe observable behavior: inputs, outputs, timings, errors. Don't describe internal algorithms unless they're part of the contract (e.g. the naming scheme).
- Every external fact gets a reference to a permitted source, or a **[verify]** tag.
- Use example data you made up or generated. Don't use captured data from third-party products.
- Keep interface definitions language-neutral where possible. The Python signatures in `providers.md` are illustrative.
