# -*- coding: utf-8 -*-
from whmcs_connector.whmcs_data_fetcher import WhmcsDataFetcher

def test_date_in_range_inclusive():
    fn = WhmcsDataFetcher._date_in_range
    assert fn("2026-09-23", "2026-09-23", "2026-09-25")
    assert fn("2026-09-25 23:59:59", "2026-09-23", "2026-09-25")
    assert not fn("2026-09-22", "2026-09-23", "2026-09-25")
    assert not fn("2026-09-26", "2026-09-23", "2026-09-25")
