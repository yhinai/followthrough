from followthrough.integrations import operational_entity


def test_research_uses_only_named_url() -> None:
    text = "Please research https://github.com/pallets/itsdangerous after this private meeting"
    assert operational_entity(text, "repository") == "https://github.com/pallets/itsdangerous"


def test_todo_keeps_bounded_action_but_redacts_credential() -> None:
    text = "I need to send the build report and the API key is sk_abcdefghijklmnopqrstuvwxyz"
    value = operational_entity(text, "todo")
    assert value == "send the build report and the [redacted credential]"
    assert "sk_" not in value


def test_todo_stops_before_unrelated_conversation() -> None:
    text = (
        "We need to verify the recovery receipt tomorrow. "
        "Then somebody discussed a confidential unrelated meeting topic."
    )
    assert operational_entity(text, "todo") == "verify the recovery receipt tomorrow"


def test_todo_without_bounded_marker_never_copies_the_raw_segment() -> None:
    raw = "A long ambient segment mentions a task but has no explicit commitment marker"
    assert operational_entity(raw, "todo") == "Review and complete the captured commitment"


def test_non_actionable_generic_text_is_not_promoted() -> None:
    assert operational_entity("We discussed ordinary lunch plans", "ordinary_life") == (
        "the identified opportunity"
    )


def test_bare_owner_repo_and_quoted_names_are_extracted() -> None:
    assert operational_entity("please evaluate pypa/sampleproject soon", "repository") == "pypa/sampleproject"
    assert operational_entity('someone mentioned "LangGraph Studio" earlier', "tool") == "LangGraph Studio"


def test_contact_keeps_bounded_clause_and_strips_filler() -> None:
    text = "Um, okay, follow up with Jordan about the invoice. Then unrelated chatter."
    assert operational_entity(text, "contact") == "Jordan about the invoice"


def test_contact_without_marker_never_copies_raw_ambient_text() -> None:
    raw = "Um if I run out of message you say"
    assert operational_entity(raw, "contact") == "Follow up on the captured contact"


def test_web_task_keeps_the_full_bounded_command() -> None:
    text = "Book the cheapest flight from CDG to NRT on Saturday. Then we chatted about lunch."
    assert operational_entity(text, "web_task") == (
        "Book the cheapest flight from CDG to NRT on Saturday"
    )


def test_start_url_lands_the_agent_on_the_named_site() -> None:
    from followthrough.integrations import start_url

    assert start_url("Check the price of the NVIDIA RTX 5080 on Best Buy") == (
        "https://www.bestbuy.com/site/searchpage.jsp?st=nvidia+rtx+5080"
    )
    # An explicit link always wins over an inferred search page.
    assert start_url("Research https://github.com/pypa/sampleproject") == (
        "https://github.com/pypa/sampleproject"
    )
    # No named site: let the agent decide where to begin.
    assert start_url("Book the cheapest flight to Tokyo") is None


def test_web_task_keeps_the_full_price_command_not_best_buy_verb_collision() -> None:
    # "Buy" inside "Best Buy" once read as the command verb. The command must
    # survive intact, minus the part addressed to Followthrough rather than to
    # the browser: the agent has no way to "tell me when you're done".
    spoken = "Followthrough, check the current RTX 5080 price on Best Buy and tell me when you're done"
    assert operational_entity(spoken, "web_task") == "check the current RTX 5080 price on Best Buy"



def test_web_command_survives_the_wake_word_and_the_best_buy_trap() -> None:
    # "Buy" inside "Best Buy" used to be read as the command verb, which sent
    # the agent off to "Buy and tell me when you are done".
    spoken = "Followthrough, check the current RTX 5080 price on Best Buy and tell me when you are done."
    assert operational_entity(spoken, "web_task") == "check the current RTX 5080 price on Best Buy"


def test_web_command_drops_trailing_instructions_to_the_assistant() -> None:
    assert operational_entity(
        "Hey Followthrough, check the price of the RTX 5080 on Best Buy, thanks", "web_task"
    ) == "check the price of the RTX 5080 on Best Buy"


def test_explicit_search_overrides_conversational_prefix_sentences() -> None:
    spoken = "No. No. No. Search the web and find how much caffeine content is in a Red Bull."
    assert operational_entity(spoken, "web_task") == (
        "Search the web and find how much caffeine content is in a Red Bull"
    )


def test_research_anchor_drops_unrelated_leading_conversation() -> None:
    spoken = "Yeah, that makes sense. Research the latest World Cup schedule. Thanks."
    assert operational_entity(spoken, "web_task") == "Research the latest World Cup schedule"


def test_start_url_query_excludes_the_wake_word_and_assistant_tail() -> None:
    from followthrough.integrations import start_url

    spoken = "Followthrough, check the current RTX 5080 price on Best Buy and tell me when you're done."
    command = operational_entity(spoken, "web_task")
    # Searching Best Buy for "followthrough ... tell me when you're done" sent
    # the agent wandering for 46 steps.
    assert start_url(command) == "https://www.bestbuy.com/site/searchpage.jsp?st=rtx+5080"


def test_general_price_question_starts_on_search_results() -> None:
    from followthrough.integrations import start_url

    assert start_url("What's the cost of gold today") == (
        "https://www.bing.com/search?q=gold+today"
    )
