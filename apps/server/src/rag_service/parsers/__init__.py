"""Document parsers for the RAG service.

Each parser exposes an ``async parse_document(file_path, output_dir, **kwargs)``
returning a MinerU-style ``content_list`` (list of dicts with ``type``,
``text``, ``img_path``, ``page_idx``, etc.) — the same shape RAGAnything's
``insert_content_list`` / ``process_document_complete`` consume.
"""
