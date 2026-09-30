"""Context reasoner for client_tags.

Rebuilds a logical tag relationship graph from the document corpus on every
run, clusters it, and re-evaluates the tags of newly created or edited files
against that context. Nothing is persisted except a small processed-files
checkpoint so the next run only looks at new (mtime or content) files.
"""
