from seminars.app import app
from seminars import db
from seminars.talk import talks_search, talks_lucky, talks_lookup
from seminars.utils import (
    Toggle,
    ics_file,
    languages_dict,
    restricted_topics as user_topics,
    subject_dict,
    subject_pairs,
    topdomain,
    topics,
    maxlength,
)
from seminars.institution import institutions, WebInstitution
from seminars.knowls import static_knowl
from flask import abort, render_template, request, redirect, url_for, Response, make_response
from seminars.seminar import seminars_search, all_seminars, all_organizers, seminars_lucky, next_talk_sorted
from flask_login import current_user
import json
from datetime import datetime, timedelta
import pytz
from collections import Counter
from dateutil.parser import parse

from lmfdb.utils import (
    BasicSpacer,
    SearchArray,
    SelectBox,
    TextBox,
    flash_error,
    to_dict,
)

from lmfdb.utils.search_parsing import collapse_ors


def get_now():
    # Returns now in UTC, comparable to time-zone aware datetimes from the database
    return datetime.now(pytz.UTC)


def parse_subject(info, query):
    subject = info.get("subject")
    if subject:
        query["subjects"] = {"$contains": subject}


def parse_topic(info, query):
    # of the talk
    topic = info.get("topic")
    if topic:
        # FIXME: temporary bridge during addition of physics
        if "_" not in topic:
            topic = "math_" + topic
        query["topics"] = {"$or": [{"$contains": topic}, {"$contains": topic[5:]}]}


def parse_institution_sem(info, query):
    inst = info.get("institution")
    if inst == "None":
        query["institutions"] = []
    elif inst:
        # one day we will do joins
        query["institutions"] = {"$contains": inst}


def parse_institution_talk(info, query):
    if info.get("institution"):
        sub_query = {}
        # one day we will do joins
        parse_institution_sem(info, sub_query)
        sem_shortnames = list(seminars_search(sub_query, "shortname"))
        query["seminar_id"] = {"$in": sem_shortnames}


def parse_venue(info, query):
    value = info.get("venue")
    if value == "online":
        query["online"] = True
    elif value == "in-person":
        query["room"] = {"$and": [{"$exists": True}, {"$ne": ""}]}


def parse_substring(info, query, field, qfields, start="%", end="%"):
    if info.get(field):
        kwds = [elt.strip() for elt in info.get(field).split(",") if elt.strip()]
        collapse_ors(
            [
                "$or",
                [{qfield: {"$ilike": start + elt + end}} for elt in kwds for qfield in qfields],
            ],
            query,
        )


def parse_access(info, query):
    # we want exact matches
    access = info.get("access")
    if access == "open":
        query["access"] = "open"
    elif access == "users":
        query["access"] = {"$or": ["open", "users"]}


def parse_daterange(info, query, time=True):
    tz = current_user.tz
    date = info.get("daterange")
    if date:
        sub_query = {}
        if "-" not in date:
            # make it into a range
            date = date + "-" + date
        start, end = date.split("-")
        if start.strip():
            try:
                start = tz.localize(parse(start))
                sub_query["$gte"] = start if time else start.date()
            except Exception as e:
                flash_error("Could not parse start date %s.  Error: " + str(e), start)
        if end.strip():
            try:
                end = tz.localize(parse(end))
                end = end + timedelta(hours=23, minutes=59, seconds=59)
                sub_query["$lte"] = end if time else end.date()
            except Exception as e:
                flash_error("Could not parse end date %s.  Error: " + str(e), end)
        if sub_query:
            query["start_time" if time else "start_date"] = sub_query

def parse_video(info, query):
    v = info.get("video")
    if v == "yes":
        query["video_link"] = {"$ne": ''}


def parse_language(info, query):
    v = info.get("language")
    if v:
        query["language"] = v


def talks_parser(info, query):
    parse_subject(info, query)
    parse_topic(info, query)
    parse_institution_talk(info, query)
    #parse_venue(info, query)
    parse_substring(info, query, "keywords",
                    ["title",
                     "abstract",
                     "speaker",
                     "speaker_affiliation",
                     "seminar_id",
                     "comments",
                     "speaker_homepage",
                     "paper_link"])
    parse_access(info, query)

    parse_substring(info, query, "speaker", ["speaker"])
    parse_substring(info, query, "affiliation", ["speaker_affiliation"])
    parse_substring(info, query, "title", ["title"])
    parse_video(info, query)
    parse_language(info, query)
    parse_daterange(info, query, time=True)
    query["display"] = True
    # TODO: remove this temporary measure allowing hidden to be None
    query["hidden"] = {"$or": [False, {"$exists": False}]}
    # These are necessary but not succificient conditions to display the talk
    # Also need that the seminar has visibility 2.

    # FIXME: temporary measure during addition of physics
    if topdomain() == "mathseminars.org":
        query["subjects"] = ["math"]


def seminars_parser(info, query, org_query={}, conference=False):
    parse_subject(info, query)
    parse_topic(info, query)
    parse_institution_sem(info, query)
    #parse_venue(info, query)
    parse_substring(info, query, "keywords",
                    ["name",
                     "description",
                     "homepage",
                     "shortname",
                     "comments"])
    org_cols = ["name", "full_name", "homepage"]
    if current_user.is_subject_admin(None):
        org_cols.append("email")
    parse_substring(info, org_query, "organizer", org_cols)
    parse_access(info, query)
    parse_language(info, query)
    if conference:
        parse_daterange(info, query, time=False)

    parse_substring(info, query, "name", ["name"])
    query["display"] = True
    query["visibility"] = 2

    # FIXME: temporary measure during addition of physics
    if topdomain() == "mathseminars.org":
        query["subjects"] = ["math"]


def institutions_shortnames():
    return sorted(
        db.institutions.search({}, projection=["shortname", "name"]), key=lambda elt: elt["name"]
    )


textwidth = 400


class TalkSearchArray(SearchArray):
    noun = "talk"
    plural_noun = "talks"

    def __init__(self):
        ## subjects
        subject = SelectBox(name="subject", label="Subject", options=[("", "")] + subject_pairs())
        ## topics
        topic = SelectBox(name="topic", label="Topic", options=[("", "")] + user_topics())

        ## pick institution where it is held
        institution = SelectBox(
            name="institution",
            label="Institution",
            options=[("", ""), ("None", "No institution",),]
            + [(elt["shortname"], elt["name"]) for elt in institutions_shortnames()],
        )

        venue = SelectBox(
            name="venue",
            label=static_knowl("venue"),
            options=[("", ""),
                     ("online", "online"),
                     ("in-person", "in-person")
                     ]
        )
        assert venue

        ## keywords for seminar or talk
        keywords = TextBox(
            name="keywords",
            label="Anywhere",
            colspan=(1, 2, 1),
            width=textwidth,
        )
        ## type of access
        access = SelectBox(
            name="access",
            label="Access",
            options=[
                ("", ""),
                ("open", "Any visitor can view link"),
                ("users", "Any logged-in user can view link"),
            ],
        )
        ## number of results to display
        # count = TextBox(name="talk_count", label="Results to display", example=50, example_value=True)

        speaker = TextBox(
            name="speaker",
            label="Speaker",
            colspan=(1, 2, 1),
            width=textwidth,
        )
        affiliation = TextBox(
            name="affiliation",
            label="Affiliation",
            colspan=(1, 2, 1),
            width=textwidth,
        )
        title = TextBox(
            name="title",
            label="Title",
            colspan=(1, 2, 1),
            width=textwidth,
        )
        date = TextBox(
            name="daterange",
            id="daterange",
            label="Date",
            example=datetime.now(current_user.tz).strftime("%B %d, %Y -"),
            example_value=True,
            colspan=(1, 2, 1),
            width=textwidth,
        )
        lang_dict = languages_dict()
        language = SelectBox(
            name="language",
            label="Language",
            options=[("", ""), ("en", "English")]
            + [
                (code, lang_dict[code])
                for code in sorted(db.talks.distinct("language"))
                if code != "en"
            ],
        )
        video = Toggle(name="video", label="Has video")
        self.array = [
            [subject, keywords],
            [topic, title],
            [institution, speaker],
            [language, affiliation],
            [access, date],
            [video]
            # [venue],
            # [count],
        ]

    def main_table(self, info=None):
        return self._print_table(self.array, info, layout_type="horizontal")

    def search_types(self, info):
        return [
            ("talks", "Search talks"),
            BasicSpacer("Times in %s" % (current_user.show_timezone("browse"))),
        ]

    def hidden(self, info):
        return []  # [("talk_start", "talk_start")]


class SemSearchArray(SearchArray):
    noun = "series"
    plural_noun = "series"

    def __init__(self, conference=False):
        ## subjects
        subject = SelectBox(name="subject", label="Subject", options=[("", "")] + subject_pairs())
        ## topics
        topic = SelectBox(name="topic", label="Topic", options=[("", "")] + user_topics())

        ## pick institution where it is held
        institution = SelectBox(
            name="institution",
            label="Institution",
            options=[("", ""), ("None", "No institution",),]
            + [(elt["shortname"], elt["name"]) for elt in institutions_shortnames()],
        )

        venue = SelectBox(
            name="venue",
            label=static_knowl("venue"),
            options=[("", ""),
                     ("online", "online"),
                     ("in-person", "in-person")
                     ]
        )
        assert venue

        ## keywords for seminar or talk
        keywords = TextBox(name="keywords", label="Anywhere", width=textwidth,)
        ## type of access
        access = SelectBox(
            name="access",
            label="Access",
            options=[
                ("", ""),
                ("open", "Any visitor can view link"),
                ("users", "Any logged-in user can view link"),
            ],
        )
        lang_dict = languages_dict()
        language = SelectBox(
            name="language",
            label="Language",
            options=[("", ""), ("en", "English")]
            + [
                (code, lang_dict[code])
                for code in sorted(db.talks.distinct("language"))
                if code != "en"
            ],
        )
        ## number of results to display
        # count = TextBox(name="seminar_count", label="Results to display", example=50, example_value=True)

        name = TextBox(
            name="name",
            label="Name",
            width=textwidth,
        )
        organizer = TextBox(
            name="organizer",
            label="Organizer",
            width=textwidth,
        )
        date = TextBox(
            name="daterange",
            id="daterange",
            label="Date",
            example=datetime.now(current_user.tz).strftime("%B %d, %Y -"),
            example_value=True,
            colspan=(1, 2, 1),
            width=textwidth,
        )
        self.array = [
            [subject, keywords],
            [topic, name],
            [institution, organizer],
            [language, ],
            [access, date] if conference else [access],
        ]

        assert conference in [True, False]
        self.conference = conference

    def main_table(self, info=None):
        return self._print_table(self.array, info, layout_type="horizontal")

    def search_types(self, info):
        return [
            ("seminars", "Search " + ("conferences" if self.conference else "seminar series")),
            BasicSpacer("Times in %s" % (current_user.show_timezone("browse"))),
        ]

    def hidden(self, info):
        return []

@app.route("/")
def index():
    return _talks_index(subsection="talks")

@app.route("/conferences")
def conf_index():
    return _series_index({"is_conference": True}, subsection="conferences", conference=True)

@app.route("/seminar_series")
def semseries_index():
    return _series_index({"is_conference": False}, subsection="semseries", conference=False)

@app.route("/past")
def past_index():
    return _talks_index(subsection="past_talks", past=True)

@app.route("/past_conferences")
def past_conf_index():
    return _series_index({"is_conference": True}, subsection="past_conferences", conference=True, past=True)

def by_subject(subject):
    subject = subject.lower()
    if subject not in subject_dict():
        return abort(404, "Subject %s not found" % subject)
    return lambda: _talks_index({"subjects": {"$contains": subject}})

def by_topic(subject, topic):
    full_topic = subject + "_" + topic
    return lambda: _talks_index({"topics": {"$contains": full_topic}})

# We don't want to intercept other routes by doing @app.route("/<subject>") etc.
for subject in subject_dict():
    app.add_url_rule("/%s/" % subject, "by_subject_%s" % subject, by_subject(subject))
for ab, name, subject in topics():
    app.add_url_rule("/%s/%s/" % (subject, ab.replace("_", ".")), "by_topic_%s_%s" % (subject, ab), by_topic(subject, ab))

def _get_counters(objects):
    topic_counts = Counter()
    language_counts = Counter()
    subject_counts = Counter()
    for object in objects:
        if object.topics:
            for topic in object.topics:
                topic_counts[topic] += 1
        if object.subjects:
            for subject in object.subjects:
                subject_counts[subject] += 1
        language_counts[object.language] += 1
    lang_dict = languages_dict()
    languages = [(code, lang_dict[code]) for code in language_counts]
    languages.sort(key=lambda x: (-language_counts[x[0]], x[1]))
    return {"topic_counts": topic_counts, "language_counts": language_counts, "subject_counts": subject_counts, "languages": languages}

def _get_row_attributes(objects):
    filtered_subjects = set(request.cookies.get('subjects', '').split(','))
    filter_subject = request.cookies.get('filter_subject', '0') != '0'
    filtered_topics = set(request.cookies.get('topics', '').split(','))
    filter_topic = request.cookies.get('filter_topic', '0') != '0'
    filtered_languages = set(request.cookies.get('languages', '').split(','))
    filter_language = request.cookies.get('filter_language', '0') != '0'
    filter_calendar = request.cookies.get('filter_calendar', '0') != '0'
    def filter_classes(obj):
        filtered = False
        classes = ['talk']

        topic_filtered = True
        for topic in obj.topics:
            classes.append("topic-" + topic)
            if topic in filtered_topics:
                topic_filtered = False
        if topic_filtered:
            classes.append('topic-filtered')
            if filter_topic:
                filtered = True

        subject_filtered = True
        for subject in obj.subjects:
            classes.append("subject-" + subject)
            if subject in filtered_subjects:
                subject_filtered = False
        if subject_filtered:
            classes.append('subject-filtered')
            if filter_subject:
                filtered = True

        classes.append("lang-" + obj.language)
        if obj.language not in filtered_languages:
            classes.append("language-filtered")
            if filter_language:
                filtered = True
        if not obj.is_subscribed():
            classes.append("calendar-filtered")
            if filter_calendar:
                filtered = True
        return classes, filtered

    attributes = []
    visible_counter = 0
    for obj in objects:
        classes, filtered = filter_classes(obj)
        if filtered:
            style = "display: none;"
        else:
            visible_counter += 1
            if visible_counter % 2: # odd
                style = "background: none;"
            else:
                style = "background: #E3F2FD;"
        row_attributes = 'class="{classes}" style="{style}"'.format(
            classes=' '.join(classes),
            style=style)
        attributes.append(row_attributes)

    return attributes


def _talks_index(query={}, sort=None, subsection=None, past=False):
    # Eventually want some kind of cutoff on which talks are included.
    query = dict(query)
    subs = subject_pairs()
    hide_filters = []
    if "subjects" in query:
        subject = query["subjects"]["$contains"]
        hide_filters = ["subject"]
        subs = ((subject, subject.capitalize()),)
    elif "topics" in query:
        hide_filters = ["subject", "topic"]
    elif topdomain() == "mathseminars.org":
        query["subjects"] = ["math"]
    query["display"] = True
    query["hidden"] = {"$or": [False, {"$exists": False}]}
    if past:
        query["end_time"] = {"$lt": datetime.now()}
        if sort is None:
            sort = [("start_time", -1), "seminar_id"]
    else:
        query["end_time"] = {"$gte": datetime.now()}
        if sort is None:
            sort = ["start_time", "seminar_id"]
    talks = list(talks_search(query, sort=sort, seminar_dict=all_seminars()))
    # Filtering on display and hidden isn't sufficient since the seminar could be private
    talks = [talk for talk in talks if talk.searchable()]
    counters = _get_counters(talks)
    row_attributes = _get_row_attributes(talks)
    return render_template(
        "browse_talks.html",
        title="Browse talks",
        hide_filters=hide_filters,
        subjects=subs,
        section="Browse",
        subsection=subsection,
        talk_row_attributes=zip(talks, row_attributes),
        past=past,
        **counters
    )

def _series_index(query, sort=None, subsection=None, conference=True, past=False):
    query = dict(query)
    query["display"] = True
    query["visibility"] = 2
    if conference:
        # Be permissive on end-date since we don't want to miss ongoing conferences, and we could have time zone differences.  Ignore the possibility that the user/conference is in Kiribati.
        recent = datetime.now().date() - timedelta(days=1)
        if past:
            query["end_date"] = {"$lt": recent}
        else:
            query["end_date"] = {"$gte": recent}
        if sort is None:
            if past:
                sort = [("end_date", -1), ("start_date", -1), "name"]
            else:
                sort = ["start_date", "end_date", "name"]
    if sort is None: # not conferences
        # We don't currently call this case in the past, but if we add it we probably
        # need a last_talk_sorted that sorts by end time of last talk in reverse order
        series = next_talk_sorted(seminars_search(query, organizer_dict=all_organizers()))
    else:
        series = list(seminars_search(query, sort=sort, organizer_dict=all_organizers()))
    counters = _get_counters(series)
    row_attributes = _get_row_attributes(series)
    title = "Browse conferences" if conference else "Browse seminar series"
    return render_template(
        "browse_series.html",
        title=title,
        hide_filters=[],
        subjects=subject_pairs(),
        section="Browse",
        subsection=subsection,
        series_row_attributes=zip(series, row_attributes),
        is_conference=conference,
        **counters
    )

@app.route("/search/seminars")
def search_seminars():
    return _search_series(conference=False)

@app.route("/search/conferences")
def search_conferences():
    # For now this is basically the same as seminars_search, but they should diverge some (e.g. search on start date)
    return _search_series(conference=True)

def _search_series(conference=False):
    info = to_dict(request.args, search_array=SemSearchArray(conference=conference))
    if "search_type" not in info:
        info["seminar_online"] = True
        info["daterange"] = info.get("daterange", datetime.now(current_user.tz).strftime("%B %d, %Y -"))
    try:
        seminar_count = int(info["seminar_count"])
        seminar_start = int(info["seminar_start"])
        if seminar_start < 0:
            seminar_start += (1 - (seminar_start + 1) // seminar_count) * seminar_count
    except (KeyError, ValueError):
        seminar_count = info["seminar_count"] = 50
        seminar_start = info["seminar_start"] = 0
    seminar_query, org_query = {"is_conference": conference}, {}
    seminars_parser(info, seminar_query, org_query, conference=conference)
    # Ideally we would do the following with a single join query, but the backend doesn't support joins yet.
    # Instead, we use a function that returns a dictionary of all next talks as a function of seminar id.
    # One downside of this approach is that we have to retrieve ALL seminars, which we're currently doing anyway.
    # The second downside is that we need to do two queries.
    if conference:
        sort = ["start_date", "end_date", "name"]
        info["results"] = seminars_search(seminar_query, organizer_dict=all_organizers(org_query), sort=sort)
    else:
        info["results"] = next_talk_sorted(seminars_search(seminar_query, organizer_dict=all_organizers(org_query)))
    subsection = "conferences" if conference else "seminars"
    title = "Search " + ("conferences" if conference else "seminar series")
    return render_template(
        "search_seminars.html",
        title=title,
        info=info,
        section="Search",
        subsection=subsection,
        bread=None,
        is_conference=conference,
    )

@app.route("/search/talks")
def search_talks():
    info = to_dict(
        request.args, search_array=TalkSearchArray()
    )
    if "search_type" not in info:
        info["talk_online"] = True
        info["daterange"] = info.get("daterange", datetime.now(current_user.tz).strftime("%B %d, %Y -")
        )
    try:
        talk_count = int(info["talk_count"])
        talk_start = int(info["talk_start"])
        if talk_start < 0:
            talk_start += (1 - (talk_start + 1) // talk_count) * talk_count
    except (KeyError, ValueError):
        talk_count = info["talk_count"] = 50
        talk_start = info["talk_start"] = 0
    talk_query = {}
    talks_parser(info, talk_query)
    talks = talks_search(
        talk_query, sort=["start_time", "speaker"], seminar_dict=all_seminars()
    )  # limit=talk_count, offset=talk_start
    # The talk query isn't sufficient since we don't do a join so don't have access to whether the seminar is private
    talks = [talk for talk in talks if talk.searchable()]
    info["results"] = talks
    return render_template(
        "search_talks.html", title="Search talks", info=info, section="Search", subsection="talks", bread=None,
    )


@app.route("/institutions/")
def list_institutions():
    section = "Manage" if current_user.is_creator else None
    return render_template(
        "institutions.html",
        title="Institutions",
        section=section,
        subsection="institutions",
        maintained_institutions=institutions({'admin':current_user.email}),
        institutions=institutions(),
        maxlength=maxlength,
    )


@app.route("/seminar/<shortname>")
def show_seminar(shortname):
    seminar = seminars_lucky({"shortname": shortname})
    if seminar is None:
        return abort(404, "Seminar not found")
    if not seminar.visible():
        flash_error("You do not have permission to view %s", seminar.name)
        return redirect(url_for("search_seminars"), 302)
    talks = seminar.talks(projection=3)
    now = get_now()
    future = []
    past = []
    for talk in talks:
        if talk.end_time >= now:
            future.append(talk)
        else:
            past.append(talk)
    future.sort(key=lambda talk: talk.start_time)
    past.sort(key=lambda talk: talk.start_time, reverse=True)
    if current_user.email in seminar.editors() or current_user.is_subject_admin(seminar):
        section = "Manage"
    else:
        section = None
    return render_template(
        "seminar.html",
        title="View series",
        future=future,
        past=past,
        seminar=seminar,
        section=section,
        subsection="view",
        bread=None,
    )


def talks_search_api(shortname, projection=1):
    query = {"seminar_id": shortname, "display": True, "hidden": {"$or": [False, {"$exists": False}]}}
    reverse_sort = False
    if 'daterange' in request.args:
        if request.args.get('daterange') == 'past':
            query["start_time"] = {'$lte': get_now()}
        elif request.args.get('daterange') == 'future':
            query["start_time"] = {'$gte': get_now()}
        else:
            parse_daterange(request.args, query, time=True)
    elif 'past' in request.args and 'future' in request.args:
        # no restriction on date
        pass
    elif 'past' in request.args:
        query["start_time"] = {'$lte': get_now()}
        reverse_sort = True
    elif 'future' in request.args:
        query["start_time"] = {'$gte': get_now()}
    talks = list(talks_search(query, projection=3))
    talks.sort(key=lambda talk: talk.start_time, reverse=reverse_sort)
    return talks

@app.route("/seminar/<shortname>/raw")
def show_seminar_raw(shortname):
    seminar = seminars_lucky({"shortname": shortname})
    if seminar is None or not seminar.visible():
        return abort(404, "Seminar not found")
    talks = talks_search_api(shortname)
    return render_template(
        "seminar_raw.html", title=seminar.name, talks=talks, seminar=seminar
    )

@app.route("/seminar/<shortname>/bare")
def show_seminar_bare(shortname):
    seminar = seminars_lucky({"shortname": shortname})
    if seminar is None or not seminar.visible():
        return abort(404, "Seminar not found")
    talks = talks_search_api(shortname)
    resp = make_response(render_template("seminar_bare.html",
                                         title=seminar.name, talks=talks,
                                         seminar=seminar,
                                         _external=( '_external' in request.args ),
                                         site_footer=( 'site_footer' in request.args ),))
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

@app.route("/seminar/<shortname>/json")
def show_seminar_json(shortname):
    seminar = seminars_lucky({"shortname": shortname})
    if seminar is None or not seminar.visible():
        return abort(404, "Seminar not found")
    # FIXME
    cols = [
        'speaker',
        'video_link',
        'slides_link',
        'title',
        'room',
        'comments',
        'abstract',
        'start_time',
        'end_time',
        'speaker_affiliation',
        'speaker_homepage',
        'language',
        'deleted',
        'paper_link',
        'stream_link',
    ]
    talks = [
        {c: getattr(elt, c) for c in cols}
        for elt in talks_search_api(shortname, projection=["seminar_id"] + cols)
    ]
    callback = request.args.get("callback", False)
    if callback:
        return Response(
            "{}({})".format(str(callback), json.dumps(talks, default=str)),
            mimetype="application/javascript",
        )
    else:
        return Response(json.dumps(talks, default=str), mimetype="application/json")

@app.route("/embeddable_schedule.js")
def show_seminar_js():
    """
    Usage example:
    <div class="embeddable_schedule" shortname="LATeN" daterange="future"></div>
    <div class="embeddable_schedule" shortname="LATeN" daterange="past"></div>
    <div class="embeddable_schedule" shortname="LATeN" daterange="future"></div>
    <script src="http://localhost:37778/embeddable_schedule.js" onload='embed_schedule();'></script>
    """
    resp = make_response(render_template('seminar_raw.js', scheme=request.scheme))
    resp.headers['Content-type'] = 'text/javascript'
    return resp

@app.route("/embed_seminars.js")
def embed_seminar_js():
    """
    Usage example:
    <div class="embeddable_schedule" shortname="LATeN" daterange="April 23, 2020 - April 29, 2020"></div>
    <div class="embeddable_schedule" shortname="LATeN" daterange="past"></div>
    <div class="embeddable_schedule" shortname="LATeN" daterange="future"></div>
    <script src="http://localhost:37778/embed_seminars.js" onload="seminarEmbedder.initialize({'addCSS': true});"></script>
    """
    resp = make_response(render_template('embed_seminars.js', scheme=request.scheme))
    resp.headers['Content-type'] = 'text/javascript'
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

@app.route("/seminar/<shortname>/ics")
def ics_seminar_file(shortname):
    seminar = seminars_lucky({"shortname": shortname})
    if seminar is None or not seminar.visible():
        return abort(404, "Seminar not found")

    return ics_file(
        seminar.talks(),
        filename="{}.ics".format(shortname),
        user=current_user)


@app.route("/talk/<seminar_id>/<int:talkid>/ics")
def ics_talk_file(seminar_id, talkid):
    talk = talks_lucky({"seminar_id": seminar_id, "seminar_ctr": talkid})
    if talk is None:
        return abort(404, "Talk not found")
    return ics_file(
        [talk],
        filename="{}_{}.ics".format(seminar_id, talkid),
        user=current_user)


@app.route("/talk/<seminar_id>/<int:talkid>/")
def show_talk(seminar_id, talkid):
    token = request.args.get("token", "")  # save the token so user can toggle between view and edit
    talk = talks_lucky({"seminar_id": seminar_id, "seminar_ctr": talkid})
    if talk is None:
        return abort(404, "Talk not found")
    kwds = dict(
        title="View talk", talk=talk, seminar=talk.seminar, subsection="viewtalk", token=token
    )
    if token:
        kwds["section"] = "Manage"
        # Also want to override top menu
        from seminars.utils import top_menu

        menu = top_menu()
        menu[2] = (url_for("create.index"), "", "Manage")
        kwds["top_menu"] = menu
    elif (
        current_user.is_subject_admin(talk)
        or current_user.email_confirmed
        and (
            current_user.email in talk.seminar.editors() or current_user.email == talk.speaker_email
        )
    ):
        kwds["section"] = "Manage"
    return render_template("talk.html", **kwds)

# We allow async queries for title knowls
@app.route("/knowl/talk/<series_id>/<int:series_ctr>")
def title_knowl(series_id, series_ctr, **kwds):
    talk = talks_lookup(series_id, series_ctr)
    if talk is None:
        return render_template("404_content.html"), 404
    else:
        return render_template("talk-knowl.html", talk=talk)


@app.route("/institution/<shortname>/")
def show_institution(shortname):
    institution = db.institutions.lookup(shortname)
    if institution is None:
        return abort(404, "Institution not found")
    institution = WebInstitution(shortname, data=institution)
    section = "Manage" if current_user.is_creator else None
    query = {"institutions": {"$contains": shortname}}
    if not current_user.is_admin:
        query["display"] = True
    events = list(
        seminars_search(
            query, sort=["weekday", "start_time", "name"], organizer_dict=all_organizers(),
        )
    )
    seminars = [S for S in events if not S.is_conference]
    conferences = [S for S in events if S.is_conference]
    conferences.sort(key=lambda S: (S.start_date, S.name))
    return render_template(
        "institution.html",
        seminars=seminars,
        conferences=conferences,
        title="View institution",
        institution=institution,
        section=section,
        subsection="viewinst",
    )


@app.route("/info")
def info():
    return render_template("info.html", title="Features", section="Info", subsection="features")

@app.route("/privacy")
def privacy():
    return render_template("privacy.html", title="Privacy policy", section="Info", subsection="privacy")

@app.route("/policies")
def policies():
    return render_template("policies.html", title="Policies", section="Info", subsection="policies")


@app.route("/faq")
def faq():
    return render_template("faq.html", title="Frequently asked questions", section="Info", subsection="faq")


@app.route("/ams")
def ams():
    seminars = next_talk_sorted(
        seminars_search(
            query={"subjects": {'$contains': "math"}},
            organizer_dict=all_organizers()
        )
    )
    from collections import defaultdict
    math_topics = {elt['subject'] + '_' + elt['abbreviation'] : elt['name'].capitalize() for elt in db.topics.search() if elt['subject'] == 'math'}
    seminars_dict = defaultdict(list)
    for sem in seminars:
        for topic in sem.topics:
            if topic in math_topics:
                seminars_dict[topic].append(sem)
    return render_template("ams.html",
                           title="AMS example",
                           math_topics=sorted(math_topics.items(), key=lambda x: x[1]),
                           seminars_dict=seminars_dict)


