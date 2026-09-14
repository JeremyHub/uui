"""How the live screen gets compacted into a prompt.

These are the decisions that trade prompt size against the model's ability to write
markup that matches the page, so they are worth pinning down precisely.
"""

from backend.screen import compact, find_regions, render_screen


def repeated(tag, n, cls="row"):
    return f"<ul>{''.join(f'<li class=\"{cls}\">Item {i}</li>' for i in range(n))}</ul>"


def test_short_runs_are_sent_verbatim():
    # Eliding a three-item list saves almost nothing and risks the model copying the
    # marker into its answer, so short runs stay whole.
    out = compact(repeated("li", 3))
    assert out.count("<li") == 3
    assert "more" not in out


def test_long_runs_collapse_to_a_pattern_plus_a_count():
    out = compact(repeated("li", 9))
    # Count the items themselves; the elision marker names the tag too.
    assert out.count('class="row"') == 4
    assert "5 more" in out


def test_runs_collapse_at_the_top_level_of_a_region_too():
    # Regions are usually a flat list of cards with no wrapper, which is exactly the
    # case an earlier version missed entirely.
    cards = "".join(f'<div class="card"><h2>Card {i}</h2></div>' for i in range(8))
    out = compact(cards)
    assert out.count('class="card"') == 4
    assert "4 more" in out


def test_different_kinds_of_sibling_do_not_collapse_together():
    mixed = "".join(f'<div class="a">{i}</div><div class="b">{i}</div>' for i in range(6))
    assert "more" not in compact(mixed)


def test_class_names_and_structure_survive():
    # The whole reason for sending markup instead of a summary: without classes the
    # model rewrites styled cards as bare unstyled divs.
    out = compact('<div class="card"><img src="https://x/a.jpg" alt="A"><h2>Hi</h2></div>')
    assert 'class="card"' in out
    assert "<img" in out and 'alt="A"' in out


def test_long_text_is_truncated_but_the_element_survives():
    out = compact(f"<p>{'word ' * 200}</p>")
    assert out.startswith("<p>") and out.endswith("</p>")
    assert len(out) < 400


def test_render_screen_stays_within_budget():
    regions = [{"id": f"r{i}", "html": "<p>%s</p>" % ("x" * 4000)} for i in range(6)]
    out = render_screen(regions, styles="body{color:red}", budget=3000)
    assert len(out) <= 3100


def test_render_screen_labels_every_region():
    out = render_screen([{"id": "results", "html": "<p>hi</p>"}], styles="")
    assert 'data-region="results"' in out and "<p>hi</p>" in out


def test_find_regions_reads_exact_inner_html():
    html = '<section data-region="a"><p class="x">one</p></section>'
    assert find_regions(html) == [{"id": "a", "html": '<p class="x">one</p>'}]


def test_find_regions_handles_void_tags_and_nesting():
    html = ('<div data-region="outer"><img src="a.png"><p>t</p>'
            '<div data-region="inner">deep</div></div>')
    found = {r["id"]: r["html"] for r in find_regions(html)}
    assert found["inner"] == "deep"
    assert "<img" in found["outer"] and "deep" in found["outer"]


def test_find_regions_keeps_document_order():
    html = "".join(f'<section data-region="r{i}">x</section>' for i in range(4))
    assert [r["id"] for r in find_regions(html)] == ["r0", "r1", "r2", "r3"]
