from explorer.wallet_release import parse_version, pick_linux_tarball, pick_windows_zip


def test_parse_version_strips_edition_suffix():
    assert parse_version("v1.0.16") == (1, 0, 16)
    assert parse_version("1.0.16-light") == (1, 0, 16)
    assert parse_version("v1.0.16-heavy") == (1, 0, 16)
    assert parse_version("v1.0.15") == (1, 0, 15)
    assert parse_version("nope") is None


def test_pick_skips_github_latest_flag_and_light_suffix():
    rels = [
        {
            "tag_name": "v1.0.14",
            "name": "X Coin 1.0.14 Light",
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "name": "X-Coin-1.0.14-Windows.zip",
                    "browser_download_url": "https://example/14.zip",
                }
            ],
        },
        {
            "tag_name": "v1.0.16-light",
            "name": "X Coin 1.0.16 Light",
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "name": "X-Coin-1.0.16-Windows.zip",
                    "browser_download_url": "https://example/16l.zip",
                }
            ],
        },
        {
            "tag_name": "v1.0.16",
            "name": "X Coin 1.0.16 Heavy",
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "name": "X-Coin-1.0.16-Windows.zip",
                    "browser_download_url": "https://example/16h.zip",
                },
                {
                    "name": "X-Coin-1.0.16-Linux-x86_64.tar.gz",
                    "browser_download_url": "https://example/16h.tgz",
                },
            ],
        },
    ]
    win = pick_windows_zip(rels)
    assert win == ("1.0.16", "https://example/16h.zip")
    linux = pick_linux_tarball(rels)
    assert linux == ("1.0.16", "https://example/16h.tgz")


def test_pick_future_numeric_version_beats_current():
    rels = [
        {
            "tag_name": "v1.0.16",
            "name": "X Coin 1.0.16 Heavy",
            "assets": [
                {
                    "name": "X-Coin-1.0.16-Windows.zip",
                    "browser_download_url": "https://example/16.zip",
                }
            ],
        },
        {
            "tag_name": "v2.0.0-light",
            "name": "X Coin 2.0.0 Light",
            "assets": [
                {
                    "name": "X-Coin-2.0.0-Windows.zip",
                    "browser_download_url": "https://example/20.zip",
                }
            ],
        },
    ]
    assert pick_windows_zip(rels) == ("2.0.0", "https://example/20.zip")
