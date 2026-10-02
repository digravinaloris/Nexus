"""
Tests des fonctions utilitaires pures (utils.py). Ne nécessite ni token
Discord ni connexion MongoDB -- s'exécute avec : pytest tests/
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import (
    parse_duration,
    check_caps,
    check_banned_words,
    check_banned_domains,
    check_any_link,
    looks_like_question,
    match_faq_entries,
    is_mention_spam,
    detect_phone_number,
    detect_address_hint,
    detect_raid_username_pattern,
    format_uptime,
)


class TestParseDuration:
    def test_minutes(self):
        assert parse_duration("30m") == 30 * 60

    def test_hours(self):
        assert parse_duration("2h") == 2 * 3600

    def test_days(self):
        assert parse_duration("1d") == 86400

    def test_weeks(self):
        assert parse_duration("1w") == 604800

    def test_seconds(self):
        assert parse_duration("45s") == 45

    def test_uppercase_and_whitespace(self):
        assert parse_duration("  2H  ") == 2 * 3600

    def test_invalid_unit(self):
        assert parse_duration("5x") == -1

    def test_invalid_format(self):
        assert parse_duration("soon") == -1

    def test_empty_string(self):
        assert parse_duration("") == -1

    def test_negative_not_matched(self):
        assert parse_duration("-5m") == -1


class TestCheckCaps:
    def test_all_caps_flagged(self):
        assert check_caps("THIS MESSAGE IS SHOUTING AT EVERYONE") is True

    def test_normal_text_not_flagged(self):
        assert check_caps("this is a normal message, nothing wrong here") is False

    def test_too_short_to_flag(self):
        # En dessous de CAPS_MIN_LENGTH (10), jamais flag même si 100% majuscules
        assert check_caps("HI") is False

    def test_mixed_case_below_threshold(self):
        assert check_caps("Hello there, how are you doing today?") is False

    def test_custom_threshold_via_cfg(self):
        content = "Some Caps Here"  # ratio de majuscules modéré
        assert check_caps(content, cfg={"automod_caps_ratio": 0.01}) is True
        assert check_caps(content, cfg={"automod_caps_ratio": 0.99}) is False


class TestCheckBannedWords:
    def test_exact_word_match(self):
        assert check_banned_words("you are a badword honestly", ["badword"]) is True

    def test_case_insensitive(self):
        assert check_banned_words("you are a BADWORD honestly", ["badword"]) is True

    def test_whole_word_only_no_substring_match(self):
        # "class" contient "ass" mais ne doit pas déclencher un faux positif
        assert check_banned_words("I love my class today", ["ass"]) is False

    def test_no_match(self):
        assert check_banned_words("this is a totally clean message", ["badword"]) is False

    def test_empty_banned_list(self):
        assert check_banned_words("anything goes here", []) is False


class TestCheckBannedDomains:
    def test_exact_domain_match(self):
        assert check_banned_domains("check http://evil.com/page", ["evil.com"]) is True

    def test_subdomain_also_blocked(self):
        assert check_banned_domains("visit http://sub.evil.com/x", ["evil.com"]) is True

    def test_unrelated_domain_not_blocked(self):
        assert check_banned_domains("visit http://notevil.com/x", ["evil.com"]) is False

    def test_no_links_in_message(self):
        assert check_banned_domains("just a normal message, no links", ["evil.com"]) is False

    def test_https_and_http_both_detected(self):
        assert check_banned_domains("https://evil.com", ["evil.com"]) is True

    def test_empty_banned_list(self):
        assert check_banned_domains("http://anything.com", []) is False


class TestCheckAnyLink:
    def test_http_link_detected(self):
        assert check_any_link("check this http://example.com") is True

    def test_https_link_detected(self):
        assert check_any_link("check this https://example.com/page") is True

    def test_no_link(self):
        assert check_any_link("just a normal message") is False

    def test_bare_domain_without_scheme_not_detected(self):
        # Volontairement : sans http(s)://, ce n'est pas traité comme un lien
        # (limitation connue, cohérente avec URL_RE utilisé ailleurs dans le projet)
        assert check_any_link("visit example.com") is False


class TestLooksLikeQuestion:
    def test_question_mark(self):
        assert looks_like_question("tu fais quoi ?") is True

    def test_french_starter_comment(self):
        assert looks_like_question("comment on fait pour rejoindre le serveur") is True

    def test_french_starter_est_ce_que(self):
        assert looks_like_question("est-ce que le bot marche encore") is True

    def test_english_starter_how(self):
        assert looks_like_question("how do I reset my warns") is True

    def test_english_starter_can_you(self):
        assert looks_like_question("can you help me with roles") is True

    def test_statement_not_a_question(self):
        assert looks_like_question("le serveur est en maintenance") is False

    def test_empty_message(self):
        assert looks_like_question("") is False


class TestMatchFaqEntries:
    def test_single_match(self):
        entries = [{"id": "1", "keywords": ["règles"], "response": "voir #règles"}]
        assert match_faq_entries("c'est quoi les règles ?", entries) == entries

    def test_multiple_keywords_one_entry(self):
        entries = [{"id": "1", "keywords": ["règles", "reglement"], "response": "voir #règles"}]
        assert match_faq_entries("le reglement du serveur ?", entries) == entries

    def test_multiple_entries_match(self):
        entries = [
            {"id": "1", "keywords": ["règles"], "response": "a"},
            {"id": "2", "keywords": ["boost"], "response": "b"},
        ]
        result = match_faq_entries("les règles et le boost ça marche comment ?", entries)
        assert result == entries

    def test_no_match(self):
        entries = [{"id": "1", "keywords": ["règles"], "response": "a"}]
        assert match_faq_entries("il fait beau aujourd'hui", entries) == []

    def test_word_boundary_no_partial_match(self):
        # "reg" ne doit pas matcher "regarder" -- mot entier uniquement
        entries = [{"id": "1", "keywords": ["reg"], "response": "a"}]
        assert match_faq_entries("je vais regarder un film", entries) == []

    def test_empty_entries(self):
        assert match_faq_entries("comment ça marche ?", []) == []


class TestIsMentionSpam:
    def test_mass_mention_always_flagged(self):
        assert is_mention_spam(mention_count=0, has_mass_mention=True, threshold=5) is True

    def test_under_threshold(self):
        assert is_mention_spam(mention_count=3, has_mass_mention=False, threshold=5) is False

    def test_at_threshold(self):
        assert is_mention_spam(mention_count=5, has_mass_mention=False, threshold=5) is True


class TestDetectPhoneNumber:
    def test_structured_international_format(self):
        assert detect_phone_number("call me at +33 6 12 34 56 78") == "high"

    def test_structured_local_format(self):
        assert detect_phone_number("06.12.34.56.78 appelle moi") == "high"

    def test_bare_digit_run_is_low_confidence(self):
        assert detect_phone_number("my id is 1234567890") == "low"

    def test_no_match(self):
        assert detect_phone_number("let's meet at 5pm tomorrow") is None

    def test_short_number_not_flagged(self):
        assert detect_phone_number("I'm 25 years old") is None


class TestDetectAddressHint:
    def test_number_plus_street_word(self):
        assert detect_address_hint("I live at 12 rue de la paix") is True

    def test_street_word_without_number(self):
        assert detect_address_hint("meet me on main street sometime") is False

    def test_number_without_street_word(self):
        assert detect_address_hint("I scored 12 points today") is False

    def test_no_match(self):
        assert detect_address_hint("hello there") is False


class TestDetectRaidUsernamePattern:
    def test_common_prefix_detected(self):
        assert detect_raid_username_pattern(["RaidBot001", "RaidBot002", "RaidBot003"]) is True

    def test_digit_suffix_pattern_detected(self):
        assert detect_raid_username_pattern(["User8291", "Member4471", "Guest9931"]) is True

    def test_normal_usernames_not_flagged(self):
        assert detect_raid_username_pattern(["Alice", "Bob", "Charlie"]) is False

    def test_too_few_to_judge(self):
        assert detect_raid_username_pattern(["RaidBot001", "RaidBot002"]) is False


class TestFormatUptime:
    def test_minutes_only(self):
        assert format_uptime(300) == "5m"

    def test_hours_and_minutes_shown_as_hours(self):
        # Sans jours, on affiche heures + minutes (voir implémentation)
        result = format_uptime(3 * 3600 + 5 * 60)
        assert "3h" in result

    def test_days_and_hours(self):
        result = format_uptime(2 * 86400 + 4 * 3600)
        assert "2d" in result and "4h" in result

    def test_zero_seconds(self):
        assert format_uptime(0) == "0m"
