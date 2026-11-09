# Third-party notices

EvoWiki's own source is licensed under the MIT License in LICENSE.
Third-party software retains its original license; this license does not
relicense third-party libraries, models, documents or user content.

- The bundled Cytoscape.js code in `src/evowiki/deep/components/vendor/` is MIT
  licensed. Its complete original notice is retained in `cytoscape.LICENSE`.
- The Obsidian plugin's npm dependencies retain their copyright and license
  notices in the installed packages and the built bundle's legal comments.
  Obsidian itself is a separate product and is not redistributed by EvoWiki.
- The plugin's direct runtime dependencies are Floating UI, eventsource-parser,
  Graphology and its layout packages, Sigma, SortableJS and WebCola (MIT),
  canvas-confetti (ISC), jsdiff (BSD-3-Clause), and DOMPurify
  (MPL-2.0 OR Apache-2.0). Their licenses remain separate from EvoWiki's MIT
  license. See `obsidian-plugin/package-lock.json` for locked versions.
- Python packages and embedding models are downloaded separately. Their
  original licenses and model cards govern those components. EvoWiki's MIT
  license does not grant additional rights in them.

The release review verifies the actual included files and notices. Local
databases, model weights, personal vaults, recordings and configuration files
are not source-release attachments.

## Demonstration image

The demonstration image `demo-mona.webp` and the corresponding embedded
fixture derive from *Mona Lisa with eigenvector* by J. Finkelstein, released
to the public domain by its author. Source and license declaration:
[Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Mona_Lisa_with_eigenvector.png).
The demo copy is resized and converted to WebP; the source attribution is
also retained in `obsidian-plugin/src/wiki/fixtures-demo-img.ts`.
