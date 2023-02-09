# =============================================================================
# Minet Youtube Search CLI Action
# =============================================================================
#
# Action searching videos using YouTube's API.
#
import casanova
from itertools import islice

from minet.cli.utils import LoadingBar
from minet.youtube import YouTubeAPIClient
from minet.youtube.constants import YOUTUBE_VIDEO_SNIPPET_CSV_HEADERS


def action(cli_args):
    enricher = casanova.enricher(
        cli_args.file,
        cli_args.output,
        add=YOUTUBE_VIDEO_SNIPPET_CSV_HEADERS,
        keep=cli_args.select,
    )

    loading_bar = LoadingBar("Searching videos", unit="video")

    client = YouTubeAPIClient(cli_args.key)

    for row, query in enricher.cells(cli_args.column, with_rows=True):
        loading_bar.print('Searching for "%s"' % query)

        searcher = client.search(query, order=cli_args.order)

        if cli_args.limit:
            searcher = islice(searcher, cli_args.limit)

        for video in searcher:
            loading_bar.update()
            enricher.writerow(row, video.as_csv_row())
