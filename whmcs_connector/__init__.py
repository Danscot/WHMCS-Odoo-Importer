# -*- coding: utf-8 -*-
"""
WHMCS Connector — Stage 1: external API communication layer.

Provides:
    WhmcsApiClient   — authenticated HTTP client for the WHMCS external API
    WhmcsDataFetcher — paginating fetcher that maps API responses to the
                       field aliases understood by the existing WhmcsParser

Run test_connection.py to validate connectivity and data shape before
integrating this layer into the Odoo import flow.
"""
from .whmcs_api_client import WhmcsApiClient, WhmcsApiError
from .whmcs_data_fetcher import WhmcsDataFetcher

__all__ = [
    "WhmcsApiClient",
    "WhmcsApiError",
    "WhmcsDataFetcher",
]
