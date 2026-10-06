"""Hybrid Leads: new IT companies from public registries into a Google Sheet."""
import warnings

# Apple's own Python links LibreSSL, which makes urllib3 print a long warning at import time. It is harmless here.
warnings.filterwarnings("ignore", message=".*OpenSSL.*")
