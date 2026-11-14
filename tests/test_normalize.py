from scope_envelope.normalize import (
    denormalize,
    extract_subject,
    forms,
    normalize,
    path_matcher,
)


def test_extract_subject_strips_event_suffix():
    assert extract_subject("Implement Kafka consumer for SearchQueryEvent") == "search query"
    assert extract_subject("Add consumer for OrderPlacedEvent please") == "order placed"


def test_extract_subject_none_without_camel_case():
    assert extract_subject("fix the flaky test") is None


def test_forms_cover_every_spelling():
    f = forms("order placed")
    assert f == {
        "snake": "order_placed",
        "upper": "ORDER_PLACED",
        "dashed": "order-placed",
        "class": "OrderPlaced",
        "camel": "orderPlaced",
        "spaced": "order placed",
        "title": "Order Placed",
    }


def test_group_id_regression_dashed_form_is_normalized():
    # v1 bug: this line was copied verbatim into an unrelated ticket and dropped the pass rate to 6/9.
    assert normalize("group_id: integration-search-query", "search query") == "group_id: integration-{dashed}"


def test_normalize_all_forms_in_one_line():
    line = "class SearchQueryConsumer: topic=search_query enable=ENABLE_SEARCH_QUERY name='Search Query'"
    assert normalize(line, "search query") == (
        "class {class}Consumer: topic={snake} enable=ENABLE_{upper} name='{title}'"
    )


def test_normalize_paths():
    assert normalize("pipeline/consumers/search_query_consumer.py", "search query") == (
        "pipeline/consumers/{snake}_consumer.py"
    )


def test_normalize_leaves_unrelated_words_alone():
    assert normalize("border = 1", "order") == "border = 1"
    assert normalize("user_activity_consumer.py", "search query") == "user_activity_consumer.py"


def test_number_prefix_becomes_placeholder():
    assert normalize("migrations/0042_add_search_query.py", "search query") == "migrations/{n}_add_{snake}.py"


def test_round_trip_to_new_subject():
    template = normalize("enable_search_query_consumer: false", "search query")
    assert denormalize(template, "order placed") == "enable_order_placed_consumer: false"


def test_path_matcher_accepts_any_migration_number():
    matcher = path_matcher("migrations/{n}_add_{snake}.py", "order placed")
    assert matcher.match("migrations/0107_add_order_placed.py")
    assert not matcher.match("migrations/0107_add_search_query.py")
