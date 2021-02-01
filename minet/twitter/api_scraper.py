# =============================================================================
# Minet Twitter API Scraper
# =============================================================================
#
# Twitter public API "scraper".
#
import re
import time
import datetime
from urllib.parse import urlencode, quote
from tenacity import (
    Retrying,
    wait_random_exponential,
    retry_if_exception_type,
    stop_after_attempt
)
from twitwi.utils import prepare_tweet

from minet.utils import (
    create_pool,
    request,
    request_json,
    nested_get
)
from minet.twitter.constants import (
    TWITTER_PUBLIC_API_DEFAULT_TIMEOUT,
    TWITTER_PUBLIC_API_AUTH_HEADER
)
from minet.twitter.exceptions import (
    TwitterGuestTokenError,
    TwitterPublicAPIRateLimitError,
    TwitterPublicAPIInvalidResponseError,
    TwitterPublicAPIParsingError
)

# =============================================================================
# Constants
# =============================================================================
TWITTER_PUBLIC_SEARCH_ENDPOINT = 'https://api.twitter.com/2/search/adaptive.json'
DEFAULT_COUNT = 100
GUEST_TOKEN_COOKIE_PATTERN = re.compile(rb'document\.cookie = decodeURIComponent\("gt=(\d+);')


# =============================================================================
# Helpers
# =============================================================================
def forge_search_url(query):
    return (
        'https://twitter.com/search?f=live&type=spelling_expansion_revert_click&q=%s' %
        quote(query)
    )


def extract_guest_token(html):
    match = GUEST_TOKEN_COOKIE_PATTERN.search(html)

    if match is None:
        return None

    return match.group(1).decode()


def ensure_guest_token(method):
    def wrapped(self, *args, **kwargs):

        if self.guest_token is None:
            self.acquire_guest_token()

        return method(self, *args, **kwargs)

    return wrapped


def create_cookie_expiration():
    return datetime.datetime.utcfromtimestamp(time.time() + 10800).strftime('%a, %d %b %Y %H:%M:%S GMT')


def forge_search_params(query, count=DEFAULT_COUNT, cursor=None):
    params = {
        'include_can_media_tag': '1',
        'include_ext_alt_text': 'true',
        'include_quote_count': 'true',
        'include_reply_count': '1',
        'tweet_mode': 'extended',
        'include_entities': 'true',
        'include_user_entities': 'true',
        'include_ext_media_availability': 'true',
        'send_error_codes': 'true',
        'simple_quoted_tweet': 'true',
        'spelling_corrections': '1',
        'ext': 'mediaStats,highlightedLabel',
        'tweet_search_mode': 'live',
        'count': count,
        'q': query
    }

    if cursor is not None:
        params['cursor'] = cursor

    return urlencode(params, quote_via=quote)


CURSOR_FIRST_POSSIBLE_PATH = [
    'timeline',
    'instructions',
    0,
    'addEntries',
    'entries',
    -1,
    'content',
    'operation',
    'cursor',
    'value'
]

CURSOR_SECOND_POSSIBLE_PATH = [
    'timeline',
    'instructions',
    -1,
    'replaceEntry',
    'entry',
    'content',
    'operation',
    'cursor',
    'value'
]


def extract_cursor_from_payload(payload):
    found_cursor = nested_get(CURSOR_FIRST_POSSIBLE_PATH, payload)

    if found_cursor is None:
        found_cursor = nested_get(CURSOR_SECOND_POSSIBLE_PATH, payload)

    return found_cursor


def payload_tweets_iter(payload):
    tweet_index = payload['globalObjects']['tweets']
    user_index = payload['globalObjects']['users']

    for instruction in payload['timeline']['instructions']:
        if 'addEntries' in instruction:
            entries = instruction['addEntries']['entries']
        elif 'replaceEntry' in instruction:
            entries = [instruction['replaceEntry']['entry']]
        else:
            continue

        for entry in entries:
            entry_id = entry['entryId']

            # Filtering tweets
            if (
                not entry_id.startswith('sq-I-t-') and
                not entry_id.startswith('tweet-')
            ):
                continue

            tweet_id = None
            tweet_info = nested_get(['content', 'item', 'content', 'tweet'], entry)

            if tweet_info is not None:

                # Skipping ads
                if 'promotedMetadata' in tweet_info:
                    continue

                tweet_id = tweet_info['id']

            else:
                tweet_info = nested_get(['content', 'item', 'content', 'tombstone'], entry)
                tweet_id = tweet_info['tweet']['id']

            if tweet_id is None:
                raise TwitterPublicAPIParsingError

            tweet = tweet_index[tweet_id]
            tweet['user'] = user_index[tweet['user_id_str']]

            yield tweet


# =============================================================================
# Main class
# =============================================================================
class TwitterAPIScraper(object):
    def __init__(self):
        self.http = create_pool(timeout=TWITTER_PUBLIC_API_DEFAULT_TIMEOUT)
        self.reset()

    def reset(self):
        self.guest_token = None
        self.cookie = None

    # TODO: tenacity + rate limited
    def request(self, url):
        return request(self.http, url, spoof_ua=True)

    def request_json(self, url, headers=None):
        return request_json(self.http, url, spoof_ua=True, headers=headers)

    def acquire_guest_token(self):
        base_url = forge_search_url('test')

        err, response = self.request(base_url)

        if err or response.status >= 400:
            raise TwitterPublicAPIInvalidResponseError

        guest_token = extract_guest_token(response.data)

        if guest_token is None:
            raise TwitterGuestTokenError

        self.guest_token = guest_token
        self.cookie = (
            response.headers['Set-Cookie'] +
            ', gt=%s; Domain=.twitter.com; Path=/; Secure ; Expires=%s' %
            (guest_token, create_cookie_expiration())
        )

    @ensure_guest_token
    def request_search(self, query, cursor=None):
        params = forge_search_params(query, cursor=cursor)
        url = '%s?%s' % (TWITTER_PUBLIC_SEARCH_ENDPOINT, params)

        headers = {
            'Authorization': TWITTER_PUBLIC_API_AUTH_HEADER,
            'X-Guest-Token': self.guest_token,
            'Cookie': self.cookie
        }

        err, response, data = self.request_json(url, headers=headers)

        if err:
            raise err

        if response.status == 429:
            self.reset()
            raise TwitterPublicAPIRateLimitError

        if response.status >= 400:
            raise TwitterPublicAPIInvalidResponseError

        cursor = extract_cursor_from_payload(data)
        tweets = []

        for tweet in payload_tweets_iter(data):

            # TODO: this should be fixed in twitwi
            tweet['gazouilloire_source'] = 'minet'

            result = prepare_tweet(tweet)

            tweets.append(next(t for t in result if t['id'] == tweet['id_str']))

        return cursor, tweets

    def search(self, query, limit=None):
        cursor = None
        i = 0

        # TODO: add before_sleep listener kwarg for logging purposes
        retryer = Retrying(
            wait=wait_random_exponential(max=60 * 3),
            retry=retry_if_exception_type(
                exception_types=(
                    TwitterPublicAPIRateLimitError,
                    TwitterPublicAPIInvalidResponseError
                )
            ),
            stop=stop_after_attempt(6)
        )

        while True:
            new_cursor, tweets = retryer(self.request_search, query, cursor)

            for tweet in tweets:
                yield tweet

                i += 1

                if limit is not None and i >= limit:
                    return

            if new_cursor is None or len(tweets) < DEFAULT_COUNT:
                return

            cursor = new_cursor
