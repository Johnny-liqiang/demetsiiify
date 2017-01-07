import functools
import json
import mimetypes
import re
import traceback
from urllib.parse import urlparse, unquote

import lxml.etree as ET
import requests
from flask import (Blueprint, abort, current_app, jsonify, make_response,
                   redirect, render_template, request, url_for)
from flask_autodoc import Autodoc
from jinja2 import evalcontextfilter, Markup, escape
from validate_email import validate_email

from . import mets
from .models import Identifier, Manifest, IIIFImage
from .tasks import queue, failed_queue, get_redis, import_mets_job
from .iiif import make_manifest_collection, make_label

PARAGRAPH_RE = re.compile(r'(?:\r\n|\r|\n){2,}')

view = Blueprint('view', __name__)
api = Blueprint('api', __name__)
iiif = Blueprint('iiif', __name__)

auto = Autodoc()


@view.app_template_filter()
@evalcontextfilter
def nl2br(eval_ctx, value):
    result = u'\n\n'.join(u'<p>%s</p>' % p
                          for p in PARAGRAPH_RE.split(escape(value)))
    if eval_ctx.autoescape:
        result = Markup(result)
    return result


@api.errorhandler(Exception)
def handle_error(error):
    return jsonify({
        'traceback': traceback.format_exc()
    }), 500


class ServerSentEvent(object):
    def __init__(self, data):
        if not isinstance(data, str):
            data = json.dumps(data)
        self.data = data
        self.event = None
        self.id = None
        self.desc_map = {
            self.data: "data",
            self.event: "event",
            self.id: "id"}

    def encode(self):
        if not self.data:
            return ""
        lines = ["%s: %s" % (v, k)
                 for k, v in self.desc_map.items() if k]
        return "%s\n\n" % "\n".join(lines)


def is_url(value):
    return bool(urlparse.urlparse(value).scheme)


def cors(origin='*'):
    """This decorator adds CORS headers to the response"""
    def decorator(f):
        @functools.wraps(f)
        def decorated_function(*args, **kwargs):
            resp = make_response(f(*args, **kwargs))
            h = resp.headers
            h['Access-Control-Allow-Origin'] = origin
            return resp
        return decorated_function
    return decorator


# View endpoints
@view.route('/view/<path:manifest_id>', methods=['GET'])
def view_endpoint(manifest_id):
    manifest = Manifest.get(manifest_id)
    if manifest is None:
        abort(404)
    else:
        return render_template('view.html',
                               label=manifest.manifest['label'],
                               manifest_uri=manifest.manifest['@id'])


@view.route('/')
def index():
    return render_template('index.html')


@view.route('/recent')
def recent():
    return render_template('recent.html')


@view.route('/about')
def about():
    return render_template('about.html')


@view.route('/apidocs')
def apidocs():
    return render_template('apidocs.html', api=auto.generate('api'),
                           iiif=auto.generate('iiif'))


# API Endpoints
@api.route('/api/recent')
@auto.doc(groups=['api'])
def api_get_recent_manifests():
    """ Get list of recently imported manifests.

    Takes a single request parameter `page_num` to specify the page to
    obtain.
    """
    page_num = int(request.args.get('page', '1'))
    if page_num < 1:
        page_num = 1
    pagination = Manifest.query.paginate(
        page=page_num, error_out=False,
        per_page=current_app.config['ITEMS_PER_PAGE'])
    return jsonify(dict(
        next_page=pagination.next_num if pagination.has_next else None,
        manifests=[
            {'id': m.id,
             'manifest': url_for('iiif.get_manifest', manif_id=m.id,
                                 _external=True),
             'preview': m.manifest['sequences'][0]['canvases'][0]['thumbnail'],
             'label': m.label,
             'metsurl': m.origin,
             'attribution': m.manifest['attribution'],
             'attribution_logo': m.manifest['logo']}
            for m in pagination.items]))


@api.route('/api/resolve/<identifier>')
@auto.doc(groups=['api'])
def api_resolve(identifier):
    """ Resolve identifier to IIIF manifest.

    Redirects to the corresponding manifest if resolving was successful,
    otherwise returns 404.
    """
    manifest_id = Identifier.resolve(identifier)
    if manifest_id is None:
        abort(404)
    else:
        return redirect(url_for('iiif.get_manifest', manif_id=manifest_id))


def _get_basic_info(mets_url):
    tree = ET.parse(mets_url)
    doc = mets.MetsDocument(tree, url=mets_url)
    doc.read_metadata()
    thumb_urls = doc._xpath(
        ".//mets:file[@MIMETYPE='image/jpeg']/mets:FLocat/@xlink:href")
    if not thumb_urls:
        thumb_urls = doc._xpath(
            ".//mets:file[@MIMETYPE='image/jpg']/mets:FLocat/@xlink:href")
    return {
        'metsurl': mets_url,
        'label': make_label(doc.metadata),
        'thumbnail': thumb_urls[0] if thumb_urls else None,
        'attribution': {
            'logo': doc.metadata['logo'],
            'owner': doc.metadata['attribution']
        }
    }


def _extract_mets_from_dfgviewer(url):
    url = unquote(url)
    mets_url = re.findall(r'set\[mets\]=(http[^&]+)', url)
    if not mets_url:
        mets_url = re.findall(r'tx_dlf\[id\]=(http.+)', url)
    if mets_url:
        return mets_url[0]
    else:
        return None


@api.route('/api/import', methods=['POST'])
@auto.doc(groups=['api'])
def api_import():
    """ Start the import process for a METS document.

    The request payload must be a JSON object with a single `url` key that
    contains the URL of the METS document to be imported.

    Instead of a METS URL, you can also specify the URL of a DFG-Viewer
    instance.

    Will return the job status as a JSON document.
    """
    mets_url = request.json.get('url')
    if re.match(r'https?://dfg-viewer.de/show/.*?', mets_url):
        mets_url = _extract_mets_from_dfgviewer(mets_url)
    resp = None
    try:
        resp = requests.head(mets_url)
    except:
        pass
    if not resp:
        return jsonify({
            'message': 'There is no METS available at the given URL.'}), 400
    job_meta = _get_basic_info(mets_url)
    job = queue.enqueue(import_mets_job, mets_url, meta=job_meta)
    job.refresh()
    status_url = url_for('api.api_task_status', task_id=job.id,
                         _external=True)
    response = jsonify(_get_job_status(job.id))
    response.status_code = 202
    response.headers['Location'] = status_url
    return response


def _get_job_status(job):
    if isinstance(job, str):
        job = queue.fetch_job(job)
        if job is None:
            job = failed_queue.fetch_job(job)
        if job is None:
            return None
    status = job.get_status()
    out = {'id': job.id,
           'status': status}
    if status != 'failed':
        out.update(job.meta)
    if status == 'failed':
        out['traceback'] = job.exc_info
    elif status == 'queued':
        job_ids = queue.get_job_ids()
        out['position'] = job_ids.index(job.id) if job.id in job_ids else None
    elif status == 'finished':
        out['result'] = job.result
    return out


@api.route('/api/tasks', methods=['GET'])
@auto.doc(groups=['api'])
def api_list_tasks():
    """ List currently enqueued import jobs.

    Does not list currently executing jobs!
    """
    return jsonify(
        {'tasks': [_get_job_status(job_id) for job_id in queue.job_ids]})


@api.route('/api/tasks/<task_id>', methods=['GET'])
@auto.doc(groups=['api'])
def api_task_status(task_id):
    """ Obtain status for a single job. """
    status = _get_job_status(task_id)
    if status:
        return jsonify(status)
    else:
        abort(404)


@api.route('/api/tasks/<task_id>/stream')
@auto.doc(groups=['api'])
def sse_stream(task_id):
    """ Obtain a Server-Sent Event (SSE) stream for a given job.

    The stream will deliver all updates to the status.
    """
    redis = get_redis()
    job = queue.fetch_job(task_id)
    if job is None:
        job = failed_queue.fetch_job(task_id)
    if job is None:
        abort(404)

    def gen(redis):
        # NOTE: This is wasteful, yes, but in order to get updates when
        #  the queue position changes, we have to check at every update of
        #  every other job
        channel_name = '__keyspace@0__:rq:job:*'
        pubsub = redis.pubsub()
        pubsub.psubscribe(channel_name)
        last_status = None
        last_id = None

        for msg in pubsub.listen():
            # To learn about queue position changes, watch for changes
            # in the currently active
            cur_id = msg['channel'].decode('utf8').split(':')[-1]
            if cur_id == last_id and last_status['status'] != 'started':
                continue
            last_id = cur_id
            status = _get_job_status(task_id)
            if status == last_status:
                continue
            yield ServerSentEvent(status).encode()
            last_status = status
    return current_app.response_class(gen(redis), mimetype="text/event-stream")


@api.route('/api/tasks/notify', methods=['POST'])
def register_email_notification():
    recipient = request.json['recipient']
    job_ids = request.json['jobs']
    if not validate_email(recipient, verify=True):
        return jsonify({'error': 'The email passed is not valid!'}), 400
    redis = get_redis()
    jobs_key = 'notifications.{}.jobs'.format(recipient)
    batch = redis.pipeline()
    batch.sadd(jobs_key, *job_ids)
    for job_id in job_ids:
        batch.sadd('recipients.{}'.format(job_id), recipient)
    batch.execute()
    return jsonify({'jobs': [e.decode('utf8')
                             for e in redis.smembers(jobs_key)]})


# IIIF Endpoints
@iiif.route('/iiif/collection/<collection_id>/<page_id>',
            defaults={'collection_id': 'index', 'page_id': 'top'})
@auto.doc(groups=['iiif'])
@cors('*')
def get_collection(collection_id, page_id):
    """ Get the collection of all IIIF manifests on this server. """
    if collection_id != 'index':
        abort(404)
    if page_id == 'top':
        page_num = None
    else:
        page_num = int(page_id[1:])
    pagination = Manifest.query.paginate(
        page=page_num,
        per_page=current_app.config['ITEMS_PER_PAGE'])
    label = "All manifests available at {}".format(
        current_app.config['SERVER_NAME'])
    return jsonify(make_manifest_collection(
        pagination, label, collection_id, page_num))


@iiif.route('/iiif/<path:manif_id>/manifest.json')
@iiif.route('/iiif/<path:manif_id>/manifest')
@auto.doc(groups=['iiif'])
@cors('*')
def get_manifest(manif_id):
    """ Obtain a single manifest. """
    manifest = Manifest.get(manif_id)
    if manifest is None:
        abort(404)
    else:
        return jsonify(manifest.manifest)


@iiif.route('/iiif/<path:manif_id>/sequence/<sequence_id>.json')
@iiif.route('/iiif/<path:manif_id>/sequence/<sequence_id>')
@auto.doc(groups=['iiif'])
@cors('*')
def get_sequence(manif_id, sequence_id):
    """ Obtain the given sequence from a manifest. """
    sequence = Manifest.get_sequence(manif_id, sequence_id)
    if sequence is None:
        abort(404)
    else:
        return jsonify(sequence)


@iiif.route('/iiif/<path:manif_id>/canvas/<canvas_id>.json')
@iiif.route('/iiif/<path:manif_id>/canvas/<canvas_id>')
@auto.doc(groups=['iiif'])
@cors('*')
def get_canvas(manif_id, canvas_id):
    """ Obtain the given canvas from a manifest. """
    canvas = Manifest.get_canvas(manif_id, canvas_id)
    if canvas is None:
        abort(404)
    else:
        return jsonify(canvas)


@iiif.route('/iiif/<path:manif_id>/annotation/<anno_id>.json')
@iiif.route('/iiif/<path:manif_id>/annotation/<anno_id>')
@auto.doc(groups=['iiif'])
@cors('*')
def get_image_annotation(manif_id, anno_id):
    """ Obtain the given image annotation from a manifest. """
    anno = Manifest.get_image_annotation(manif_id, anno_id)
    if anno is None:
        abort(404)
    else:
        return jsonify(anno)


@iiif.route('/iiif/<path:manif_id>/range/<range_id>.json')
@iiif.route('/iiif/<path:manif_id>/range/<range_id>')
@auto.doc(groups=['iiif'])
@cors('*')
def get_range(manif_id, range_id):
    """ Obtain the given range from a manifest. """
    range_ = Manifest.get_range(manif_id, range_id)
    if range_ is None:
        abort(404)
    else:
        return jsonify(range_)


@iiif.route('/iiif/image/<image_id>/info.json')
@auto.doc(groups=['iiif'])
@cors('*')
def get_image_info(image_id):
    """ Obtain the info.json for the given image. """
    img = IIIFImage.get(image_id)
    if img is None:
        abort(404)
    else:
        return jsonify(img.info)


@iiif.route(
    '/iiif/image/<image_id>/<region>/<size>/<rotation>/<quality>.<format>')
@auto.doc(groups=['iiif'])
@cors('*')
def get_image(image_id, region, size, rotation, quality, format):
    """ Obtain a redirect to the image resource for the given IIIF Image API
        request. """
    not_supported = (region != 'full'
                     or rotation != '0'
                     or quality not in ('default', 'native'))
    if not_supported:
        abort(501)

    iiif_image = IIIFImage.get(image_id)
    if iiif_image is None:
        abort(404)

    format = mimetypes.types_map.get('.' + format)
    query = dict(format_=format)
    if size not in ('full', 'max'):
        parts = [v for v in size.split(',') if v]
        if size.endswith(','):
            query['width'] = int(parts[0])
        elif size.startswith(','):
            query['height'] = int(parts[0])
        else:
            query['width'], query['height'] = [int(p) for p in parts]
    url = iiif_image.get_image_url(**query)
    if url is None:
        abort(501)
    else:
        return redirect(url, 303)
