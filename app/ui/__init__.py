"""The presentation layer: the Streamlit web app.

This package holds ONLY presentation code. It reads scenarios through the loader,
schedules them through the factory, and renders the result -- it contains no
scheduling, simulation or scoring logic of its own. Keeping the UI in its own
layer means the exact same engine powers the CLI (``main_prod.py``) and the web
app, and the UI could be replaced wholesale without touching the domain.
"""
