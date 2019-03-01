# =============================================================================
# Minet Extract Content CLI Action
# =============================================================================
#
# Logic of the extract action.
#
import csv
import codecs
import warnings
from os.path import join
from multiprocessing import Pool
from tqdm import tqdm
from dragnet import extract_content

from minet.cli.utils import custom_reader

OUTPUT_ADDITIONAL_HEADERS = ['dragnet_error', 'dragnet_text']

ERROR_REPORTERS = {
    UnicodeDecodeError: 'wrong-encoding'
}


def worker(payload):

    line, path, encoding = payload

    # Reading file
    with codecs.open(path, 'r', encoding=encoding) as f:
        try:
            raw_html = f.read()
        except UnicodeDecodeError as e:
            return e, line, None

    # Attempting extraction
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            content = extract_content(raw_html)
    except BaseException as e:
        # TODO: I don't know yet what can happen
        raise

    return None, line, content


def extract_action(namespace):

    output_file = open(namespace.output, 'w')

    input_headers, pos, reader = custom_reader(namespace.report, ('status', 'filename', 'encoding'))

    selected_fields = namespace.select.split(',') if namespace.select else None
    selected_pos = [input_headers.index(h) for h in selected_fields] if selected_fields else None

    output_headers = (input_headers if not selected_pos else [input_headers[i] for i in selected_pos])
    output_headers += OUTPUT_ADDITIONAL_HEADERS
    output_writer = csv.writer(output_file)
    output_writer.writerow(output_headers)

    def files():
        for line in reader:
            status = int(line[pos.status]) if line[pos.status] else None

            if status is None or status >= 400:
                output_writer.writerow(line)
                loading_bar.update()
                continue

            path = join(namespace.input_directory, line[pos.filename])
            encoding = line[pos.encoding].strip() or 'utf-8'

            yield line, path, encoding


    loading_bar = tqdm(
        desc='Extracting content',
        total=namespace.total,
        dynamic_ncols=True,
        unit=' docs'
    )

    with Pool(namespace.processes) as pool:
        for error, line, content in pool.imap_unordered(worker, files()):
            loading_bar.update()

            if error is not None:
                message = ERROR_REPORTERS.get(type(error), repr(error))
                line.extend([message, ''])
                output_writer.writerow(line)
                continue

            line.extend(['', content])
            output_writer.writerow(line)

    output_file.close()
