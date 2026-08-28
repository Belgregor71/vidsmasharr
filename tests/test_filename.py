from app.identity.filename import clean_title, normalise, parse


class TestCleanTitle:
    def test_strips_release_metadata(self):
        assert clean_title("The.Show.Name.2019.1080p.WEB-DL.x265-GROUP") == "The Show Name"

    def test_strips_bracketed_tags(self):
        assert clean_title("[SubGroup] Some Anime [1080p][HEVC]") == "Some Anime"

    def test_keeps_numbers_that_are_part_of_the_title(self):
        # A bare "2" is a junk token only as a stray channel fragment; as part
        # of a title it must survive.
        assert clean_title("Blade Runner 2049 (2017)") == "Blade Runner 2049"

    def test_trailing_year_removed(self):
        assert clean_title("Arrival (2016)") == "Arrival"


class TestNormalise:
    def test_articles_and_punctuation_ignored(self):
        assert normalise("The Matrix") == normalise("Matrix")
        assert normalise("Spider-Man: No Way Home") == normalise("spider man no way home")


class TestEpisodes:
    def test_standard_sxxexx(self):
        got = parse("/media/tv/The Show/Season 02/The.Show.S02E05.1080p.mkv")
        assert (got.kind, got.name, got.season, got.episode) == ("episode", "The Show", 2, 5)

    def test_lowercase_and_x_form(self):
        got = parse("/media/tv/Some Show/some.show.3x11.hdtv.mkv")
        assert (got.season, got.episode) == (3, 11)

    def test_multi_episode_takes_the_first(self):
        got = parse("/media/tv/Show/Season 1/Show.S01E01E02.mkv")
        assert (got.season, got.episode) == (1, 1)

    def test_show_name_comes_from_directory_when_filename_is_bare(self):
        got = parse("/media/tv/Skeleton Crew/Season 01/s01e03.mkv")
        assert got.name == "Skeleton Crew"
        assert (got.season, got.episode) == (1, 3)

    def test_season_folder_is_not_mistaken_for_the_show(self):
        got = parse("/media/tv/The Bear/Season 02/The.Bear.S02E01.mkv")
        assert got.name == "The Bear"

    def test_dated_episode_identifies_show_but_not_number(self):
        got = parse("/media/tv/Daily Show/Daily.Show.2024.01.05.1080p.mkv")
        assert got.kind == "episode"
        assert got.air_date == "2024-01-05"
        assert got.season is None and got.episode is None
        # Weaker than a numbered match, and must say so.
        assert got.confidence < 0.75


class TestMovies:
    def test_folder_with_year_preferred(self):
        got = parse("/media/movies/Arrival (2016)/Arrival.2016.1080p.BluRay.x264-AMIABLE.mkv")
        assert (got.kind, got.name, got.year) == ("movie", "Arrival", 2016)

    def test_bare_filename_with_year(self):
        got = parse("/media/movies/Heat.1995.1080p.BluRay.x264.mkv")
        assert (got.name, got.year) == ("Heat", 1995)

    def test_no_year_is_low_confidence(self):
        got = parse("/media/movies/some_random_rip.mkv")
        assert got.confidence < 0.5

    def test_nothing_usable(self):
        got = parse("/media/movies/1080p.mkv")
        assert not got.is_usable

    def test_never_outranks_a_database_match(self):
        # Nothing the parser produces may claim certainty; Plex and the *arrs
        # are authoritative and resolve at 1.0.
        for sample in [
            "/media/tv/The Show/Season 02/The.Show.S02E05.mkv",
            "/media/movies/Arrival (2016)/Arrival.2016.mkv",
        ]:
            assert parse(sample).confidence < 1.0
