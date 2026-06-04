"""Step 1 address-validation pipeline package.

The individual phase modules import each other by bare module name
(e.g. ``import scrape_websites``), so the pipeline directory must be on
``sys.path`` before importing ``address_pipeline``. ``backend/main.py``
handles this when it launches a job.
"""
