from flask import Flask, Response, abort, request, jsonify, g, url_for, render_template, flash
from flask_mail import Mail, Message
from flask_sslify import SSLify
from flask_sqlalchemy import SQLAlchemy
from flask_httpauth import HTTPBasicAuth
from sqlalchemy import exc, and_, desc
from database import db_session
from celery import Celery
from datetime import datetime
from models import User, Lead, AppendedVisitor, GlobalDashboard
from twilio.rest import Client
import config
import json
import random
import hashlib
import hmac
import time


# debug
debug = config.DEBUG

# app config
app = Flask(__name__)
sslify = SSLify(app)
app.config['SECRET_KEY'] = config.SECRET_KEY

# Flask-Mail configuration
app.config['MAIL_SERVER'] = 'smtp.mailgun.org'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = config.MAIL_USERNAME
app.config['MAIL_PASSWORD'] = config.MAIL_PASSWORD
app.config['MAIL_DEFAULT_SENDER'] = config.MAIL_DEFAULT_SENDER

# SQLAlchemy
app.config['SQLALCHEMY_DATABASE_URI'] = config.SQLALCHEMY_DATABASE_URI
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = config.SQLALCHEMY_TRACK_MODIFICATIONS
db = SQLAlchemy(app)

# disable strict slashes
app.url_map.strict_slashes = False

# Celery config
app.config['CELERY_BROKER_URL'] = config.CELERY_BROKER_URL
app.config['CELERY_RESULT_BACKEND'] = config.CELERY_RESULT_BACKEND
app.config['CELERY_ACCEPT_CONTENT'] = config.CELERY_ACCEPT_CONTENT

# Initialize Celery
celery = Celery(app.name, broker=app.config['CELERY_BROKER_URL'])
celery.conf.update(app.config)

# Config mail
mail = Mail(app)

# auth
auth = HTTPBasicAuth()

# mailgun_api_key
mailgun_api_key = config.MAILGUN_API_KEY


# clear all db sessions at the end of each request
@app.teardown_appcontext
def shutdown_session(exception=None):
    db_session.remove()


# tasks sections, for async functions, etc...
@celery.task(serializer='pickle')
def send_async_email(msg):
    """Background task to send an email with Flask-Mail."""
    with app.app_context():
        mail.send(msg)


# default routes
@app.route('/', methods=['GET'])
def site_root():
    """
    Server a nicely formatted EARL API webpage
    :return: template
    """

    # page vars
    today = datetime.now()
    title = 'EARL API'

    return render_template(
        'home.html',
        today=get_date(),
        title=title
    )


@app.route('/api', methods=['GET'])
@app.route('/api/v1.0', methods=['GET'])
@app.route('/api/v1.0/index', methods=['GET'])
def index():
    """
    The default API view.  List routes:
    :return: dict
    """
    api_routes = {}
    api_routes['delivered'] = '/api/v1.0/webhooks/mailgun/lead/delivered'
    api_routes['dropped'] = '/api/v1.0/webhooks/mailgun/lead/dropped'
    api_routes['hard-bounce'] = '/api/v1.0/webhooks/mailgun/lead/hard-bounce'
    api_routes['spam-complaint'] = '/api/v1.0/webhooks/mailgun/lead/spam-complaint'
    api_routes['unsubscribe'] = '/api/v1.0/webhooks/mailgun/lead/unsubscribe'
    api_routes['clicks'] = '/api/v1.0/webhooks/mailgun/lead/click'
    api_routes['opens'] = '/api/v1.0/webhooks/mailgun/lead/open'

    # return the response
    return jsonify(api_routes), 200


@app.route('/api/v1.0/automation/dashboard/health/status')
def check_earl_health():
    """
    Administrative function to compare the health of the EARL system.
    :return: None or send_alerts()
    """

    # function variables
    g1, g2, a1, a2 = range(4)
    resp = {"EARL Health": "OK!"}
    data = None
    dashboards = db_session.query(GlobalDashboard.total_unique_visitors,
                                  GlobalDashboard.total_appends).order_by(GlobalDashboard.id.desc()).limit(2).all()

    for idx, dashboard in enumerate(dashboards):
        if idx == 0:
            g1 = int(dashboard[0])
            a1 = int(dashboard[1])
        else:
            g2 = int(dashboard[0])
            a2 = int(dashboard[1])

    # compare the values from the query and trip alerts
    # if duplicate values are found
    if g1 and g2 > 4:
        if compare_(g1, g2) is True:

            # set up email
            alert_email = config.ALERT_EMAIL
            msg_subject = "EARL Automation ALERT!"
            msg_body = "EARL Automation - Health Check Failed!"

            # send email async with celery
            send_email(alert_email, msg_subject, msg_body)

            # continue comparison and trip SMS
            if a1 and a2 > 4:
                if compare_(a1, a2) is True:
                    resp = send_alerts()

    # return api response
    if resp:
        data = resp

    return jsonify(resp), 200


@app.route('/api/v1.0/webhooks/mailgun/delivered', methods=['POST'])
def lead_delivered_json():
    """
    MG route for delivered
    :return: json
    """

    if request.method == 'POST':
        data = request.get_json()
        return jsonify(data), 200


@app.route('/api/v1.0/webhooks/mailgun/lead/delivered', methods=['POST'])
def lead_delivered():
    """
    The lead email delivered webhook.
    :return: json
    """
    if request.method == 'POST':

        form_data = {
            "message_id": request.form.get('Message-Id', None),
            "x_mail_gun_sid": request.form.get('X-Mailgun-Sid', None),
            "domain": request.form.get('domain', 'mail.earlbdc.com'),
            "event": request.form.get('event', 'delivered'),
            "timestamp": request.form.get('timestamp', None),
            "recipient": request.form.get('recipient', None),
            "signature": request.form.get('signature', None),
            "token": request.form.get('token', None)
        }

        # verify the mailgun token and signature with the api_key
        token = form_data['token'].encode('utf-8')
        timestamp = form_data['timestamp'].encode('utf-8')
        signature = form_data['signature'].encode('utf-8')
        mg_recipient = form_data['recipient']
        event = form_data['event']

        if verify(mailgun_api_key, token, timestamp, signature):

            try:
                av = db_session.query(AppendedVisitor).filter(
                    AppendedVisitor.email == mg_recipient
                ).first()

                if av:

                    try:
                        lead = db_session.query(Lead).filter(
                            Lead.appended_visitor_id == av.id
                        ).one()

                        if lead:
                            email = av.email
                            av_id = lead.appended_visitor_id
                            event = form_data['event']

                            # set the delivered flags in the database
                            lead.followup_email_delivered = 1
                            lead.followup_email_status = event
                            lead.webhook_last_update = datetime.now()
                            db_session.commit()

                            # return a successful response
                            return jsonify({"v_id": av_id, "email": email, "event": event, "status": 'success'}), 202

                        # return 404 for lead not found
                        else:
                            resp = {"Error": "Lead not found..."}
                            data = json.dumps(resp)
                            return Response(data, status=404, mimetype='application/json')

                    # database exception
                    except exc.SQLAlchemyError as err:
                        resp = {"Database Error": str(err)}
                        data = json.dumps(resp)
                        return Response(data, status=500, mimetype='application/json')

                else:
                    # return 404: no email for recipient email address
                    resp = {"Error": "Unable to resolve the recipient email address..."}
                    data = json.dumps(resp)
                    return Response(data, status=404, mimetype='application/json')

            # database exception
            except exc.SQLAlchemyError as err:
                resp = {"Database Error": str(err)}
                data = json.dumps(resp)
                return Response(data, status=500, mimetype='application/json')

        # signature and token verification failed
        else:
            resp = {"Signature": form_data['signature'], "Token": form_data['token']}
            data = json.dumps(resp)
            return Response(data, status=409, mimetype='application/json')

    else:
        # method not allowed
        resp = {"Message": "Method Not Allowed"}
        data = json.dumps(resp)
        return Response(data, status=405, mimetype='application/json')


@app.route('/api/v1.0/webhooks/mailgun/lead/dropped', methods=['POST'])
def lead_dropped():
    """
    The lead email dropped webhook
    :return: json
    """
    if request.method == 'POST':

        form_data = {
            "message_id": request.form.get('Message-Id', None),
            "x_mail_gun_sid": request.form.get('X-Mailgun-Sid', None),
            "domain": request.form.get('domain', 'mailearlbdc.com'),
            "event": request.form.get('event', 'dropped'),
            "timestamp": request.form.get('timestamp', None),
            "recipient": request.form.get('recipient', None),
            "signature": request.form.get('signature', None),
            "token": request.form.get('token', None),
            "reason": request.form.get('reason', None),
            "code": request.form.get('code', None),
            "description": request.form.get('description', None)
        }

        # verify the mailgun token and signature with the api_key
        token = form_data['token'].encode('utf-8')
        timestamp = form_data['timestamp'].encode('utf-8')
        signature = form_data['signature'].encode('utf-8')
        mg_recipient = form_data['recipient']
        event = form_data['event']
        reason = form_data['reason']
        code = form_data['code']
        description = form_data['description']

        if verify(mailgun_api_key, token, timestamp, signature):

            try:
                av = db_session.query(AppendedVisitor).filter(
                    AppendedVisitor.email == mg_recipient
                ).first()

                if av:

                    try:
                        lead = db_session.query(Lead).filter(
                            Lead.appended_visitor_id == av.id
                        ).one()

                        if lead:
                            email = av.email
                            av_id = lead.appended_visitor_id
                            event = form_data['event']

                            # set the dropped flags in the database
                            lead.followup_email_delivered = 0
                            lead.followup_email_status = event
                            lead.followup_email_dropped = 1
                            lead.dropped_code = code
                            lead.dropped_reason = reason
                            lead.dropped_description = description
                            lead.webhook_last_update = datetime.now()
                            db_session.commit()

                            # return a successful response
                            return jsonify({"v_id": av_id, "email": email, "event": event, "status": 'success'}), 202

                        # return 404 for lead not found
                        else:
                            resp = {"Error": "Lead not found..."}
                            data = json.dumps(resp)
                            return Response(data, status=404, mimetype='application/json')

                    # database exception
                    except exc.SQLAlchemyError as err:
                        resp = {"Database Error": str(err)}
                        data = json.dumps(resp)
                        return Response(data, status=500, mimetype='application/json')

                else:
                    # return 404: no email for recipient email address
                    resp = {"Error": "Unable to resolve the recipient email address..."}
                    data = json.dumps(resp)
                    return Response(data, status=404, mimetype='application/json')

            # database exception
            except exc.SQLAlchemyError as err:
                resp = {"Database Error": str(err)}
                data = json.dumps(resp)
                return Response(data, status=500, mimetype='application/json')

        # signature and token verification failed
        else:
            resp = {"Signature": form_data['signature'], "Token": form_data['token']}
            data = json.dumps(resp)
            return Response(data, status=409, mimetype='application/json')

    else:
        # method not allowed
        resp = {"Message": "Method Not Allowed"}
        data = json.dumps(resp)
        return Response(data, status=405, mimetype='application/json')


@app.route('/api/v1.0/webhooks/mailgun/lead/hard-bounce', methods=['POST'])
def lead_hard_bounce():
    """
    The lead email hard bounce webhook
    :return: json
    """
    if request.method == 'POST':

        form_data = {
            "message_id": request.form.get('Message-Id', None),
            "x_mail_gun_sid": request.form.get('X-Mailgun-Sid', None),
            "domain": request.form.get('domain', 'mail.earlbdc.com'),
            "event": request.form.get('event', 'bounce'),
            "timestamp": request.form.get('timestamp', None),
            "recipient": request.form.get('recipient', None),
            "signature": request.form.get('signature', None),
            "token": request.form.get('token', None),
            "code": request.form.get('code', None),
            "error": request.form.get('error', None)
        }

        # verify the mailgun token and signature with the api_key
        token = form_data['token'].encode('utf-8')
        timestamp = form_data['timestamp'].encode('utf-8')
        signature = form_data['signature'].encode('utf-8')
        mg_recipient = form_data['recipient']
        event = form_data['event']
        code = form_data['code']
        bounce_error = form_data['error']

        if verify(mailgun_api_key, token, timestamp, signature):

            try:
                av = db_session.query(AppendedVisitor).filter(
                    AppendedVisitor.email == mg_recipient
                ).first()

                if av:

                    try:
                        lead = db_session.query(Lead).filter(
                            Lead.appended_visitor_id == av.id
                        ).one()

                        if lead:
                            email = av.email
                            av_id = lead.appended_visitor_id
                            event = form_data['event']

                            # set the dropped flags in the database
                            lead.followup_email_delivered = 0
                            lead.followup_email_status = event
                            lead.followup_email_bounced = 1
                            lead.dropped_code = code
                            lead.bounce_error = bounce_error
                            lead.webhook_last_update = datetime.now()
                            db_session.commit()

                            # return a successful response
                            return jsonify({"v_id": av_id, "email": email, "event": event, "status": 'success'}), 202

                        # return 404 for lead not found
                        else:
                            resp = {"Error": "Lead not found..."}
                            data = json.dumps(resp)
                            return Response(data, status=404, mimetype='application/json')

                    # database exception
                    except exc.SQLAlchemyError as err:
                        resp = {"Database Error": str(err)}
                        data = json.dumps(resp)
                        return Response(data, status=500, mimetype='application/json')

                else:
                    # return 404: no email for recipient email address
                    resp = {"Error": "Unable to resolve the recipient email address..."}
                    data = json.dumps(resp)
                    return Response(data, status=404, mimetype='application/json')

            # database exception
            except exc.SQLAlchemyError as err:
                resp = {"Database Error": str(err)}
                data = json.dumps(resp)
                return Response(data, status=500, mimetype='application/json')

        # signature and token verification failed
        else:
            resp = {"Signature": form_data['signature'], "Token": form_data['token']}
            data = json.dumps(resp)
            return Response(data, status=409, mimetype='application/json')

    else:
        # method not allowed
        resp = {"Message": "Method Not Allowed"}
        data = json.dumps(resp)
        return Response(data, status=405, mimetype='application/json')


@app.route('/api/v1.0/webhooks/mailgun/lead/spam-complaint', methods=['POST'])
def lead_spam_complaint():
    """
    The lead spam complaint webhook
    :return: json
    """
    if request.method == 'POST':

        form_data = {
            "message_id": request.form.get('Message-Id', None),
            "x_mail_gun_sid": request.form.get('X-Mailgun-Sid', None),
            "domain": request.form.get('domain', 'mail.earlbdc.com'),
            "event": request.form.get('event', 'spam-complaint'),
            "timestamp": request.form.get('timestamp', None),
            "recipient": request.form.get('recipient', None),
            "signature": request.form.get('signature', None),
            "token": request.form.get('token', None)
        }

        # verify the mailgun token and signature with the api_key
        token = form_data['token'].encode('utf-8')
        timestamp = form_data['timestamp'].encode('utf-8')
        signature = form_data['signature'].encode('utf-8')
        mg_recipient = form_data['recipient']
        event = form_data['event']

        if verify(mailgun_api_key, token, timestamp, signature):

            try:
                av = db_session.query(AppendedVisitor).filter(
                    AppendedVisitor.email == mg_recipient
                ).first()

                if av:

                    try:
                        lead = db_session.query(Lead).filter(
                            Lead.appended_visitor_id == av.id
                        ).one()

                        if lead:
                            email = av.email
                            av_id = lead.appended_visitor_id
                            event = form_data['event']

                            # set the dropped flags in the database
                            lead.followup_email_delivered = 0
                            lead.followup_email_status = event
                            lead.followup_email_spam = 1
                            lead.webhook_last_update = datetime.now()
                            db_session.commit()

                            # return a successful response
                            return jsonify({"v_id": av_id, "email": email, "event": event, "status": 'success'}), 202

                        # return 404 for lead not found
                        else:
                            resp = {"Error": "Lead not found..."}
                            data = json.dumps(resp)
                            return Response(data, status=404, mimetype='application/json')

                    # database exception
                    except exc.SQLAlchemyError as err:
                        resp = {"Database Error": str(err)}
                        data = json.dumps(resp)
                        return Response(data, status=500, mimetype='application/json')

                else:
                    # return 404: no email for recipient email address
                    resp = {"Error": "Unable to resolve the recipient email address..."}
                    data = json.dumps(resp)
                    return Response(data, status=404, mimetype='application/json')

            # database exception
            except exc.SQLAlchemyError as err:
                resp = {"Database Error": str(err)}
                data = json.dumps(resp)
                return Response(data, status=500, mimetype='application/json')

        # signature and token verification failed
        else:
            resp = {"Signature": form_data['signature'], "Token": form_data['token']}
            data = json.dumps(resp)
            return Response(data, status=409, mimetype='application/json')

    else:
        # method not allowed
        resp = {"Message": "Method Not Allowed"}
        data = json.dumps(resp)
        return Response(data, status=405, mimetype='application/json')


@app.route('/api/v1.0/webhooks/mailgun/lead/unsubscribe', methods=['POST'])
def lead_unsubscribe():
    """
    The lead unsubscribe dropped webhook
    :return: json
    """
    if request.method == 'POST':

        form_data = {
            "message_id": request.form.get('Message-Id', None),
            "x_mail_gun_sid": request.form.get('X-Mailgun-Sid', None),
            "domain": request.form.get('domain', 'mail.earlbdc.com'),
            "event": request.form.get('event', 'unsubscribe'),
            "timestamp": request.form.get('timestamp', None),
            "recipient": request.form.get('recipient', None),
            "signature": request.form.get('signature', None),
            "token": request.form.get('token', None)
        }

        # verify the mailgun token and signature with the api_key
        token = form_data['token'].encode('utf-8')
        timestamp = form_data['timestamp'].encode('utf-8')
        signature = form_data['signature'].encode('utf-8')
        mg_recipient = form_data['recipient']
        event = form_data['event']

        if verify(mailgun_api_key, token, timestamp, signature):

            try:
                av = db_session.query(AppendedVisitor).filter(
                    AppendedVisitor.email == mg_recipient
                ).first()

                if av:

                    try:
                        lead = db_session.query(Lead).filter(
                            Lead.appended_visitor_id == av.id
                        ).one()

                        if lead:
                            email = av.email
                            av_id = lead.appended_visitor_id
                            event = form_data['event']

                            # set the dropped flags in the database
                            lead.followup_email_delivered = 0
                            lead.followup_email_status = event
                            lead.followup_email_unsub = 1
                            lead.webhook_last_update = datetime.now()
                            db_session.commit()

                            # return a successful response
                            return jsonify({"v_id": av_id, "email": email, "event": event, "status": 'success'}), 202

                        # return 404 for lead not found
                        else:
                            resp = {"Error": "Lead not found..."}
                            data = json.dumps(resp)
                            return Response(data, status=404, mimetype='application/json')

                    # database exception
                    except exc.SQLAlchemyError as err:
                        resp = {"Database Error": str(err)}
                        data = json.dumps(resp)
                        return Response(data, status=500, mimetype='application/json')

                else:
                    # return 404: no email for recipient email address
                    resp = {"Error": "Unable to resolve the recipient email address..."}
                    data = json.dumps(resp)
                    return Response(data, status=404, mimetype='application/json')

            # database exception
            except exc.SQLAlchemyError as err:
                resp = {"Database Error": str(err)}
                data = json.dumps(resp)
                return Response(data, status=500, mimetype='application/json')

        # signature and token verification failed
        else:
            resp = {"Signature": form_data['signature'], "Token": form_data['token']}
            data = json.dumps(resp)
            return Response(data, status=409, mimetype='application/json')

    else:
        # method not allowed
        resp = {"Message": "Method Not Allowed"}
        data = json.dumps(resp)
        return Response(data, status=405, mimetype='application/json')


@app.route('/api/v1.0/webhooks/mailgun/lead/click', methods=['POST'])
def lead_clicks():
    """
    The lead email clicks webhook
    :return: json
    """
    if request.method == 'POST':

        form_data = {
            "message_id": request.form.get('Message-Id', None),
            "x_mail_gun_sid": request.form.get('X-Mailgun-Sid', None),
            "domain": request.form.get('domain', None),
            "event": request.form.get('event', 'click'),
            "timestamp": request.form.get('timestamp', None),
            "recipient": request.form.get('recipient', None),
            "signature": request.form.get('signature', None),
            "token": request.form.get('token', None),
            "ip": request.form.get('ip', None),
            "device_type": request.form.get('device-type', None),
            "client_type": request.form.get('client-type', None),
            "campaign_name": request.form.get('campaign-name', None)
        }

        # verify the mailgun token and signature with the api_key
        token = form_data['token'].encode('utf-8')
        timestamp = form_data['timestamp'].encode('utf-8')
        signature = form_data['signature'].encode('utf-8')
        mg_recipient = form_data['recipient']
        event = form_data['event']
        ip_addr = form_data['ip']
        device_type = form_data['device_type']
        campaign_name = form_data['campaign_name']
        client_type = form_data['client_type']

        if verify(mailgun_api_key, token, timestamp, signature):

            try:
                av = db_session.query(AppendedVisitor).filter(
                    AppendedVisitor.email == mg_recipient
                ).first()

                if av:

                    try:
                        lead = db_session.query(Lead).filter(
                            Lead.appended_visitor_id == av.id
                        ).one()

                        if lead:
                            email = av.email
                            av_id = lead.appended_visitor_id
                            event = form_data['event']

                            # set the dropped flags in the database
                            lead.followup_email_delivered = 0
                            lead.followup_email_status = event
                            lead.followup_email_clicks += lead.followup_email_clicks
                            lead.followup_email_click_ip = ip_addr
                            lead.followup_email_click_campaign = campaign_name
                            lead.followup_email_click_device = device_type
                            lead.webhook_last_update = datetime.now()
                            db_session.commit()

                            # return a successful response
                            return jsonify({"v_id": av_id, "email": email, "event": event, "status": 'success'}), 202

                        # return 404 for lead not found
                        else:
                            resp = {"Error": "Lead not found..."}
                            data = json.dumps(resp)
                            return Response(data, status=404, mimetype='application/json')

                    # database exception
                    except exc.SQLAlchemyError as err:
                        resp = {"Database Error": str(err)}
                        data = json.dumps(resp)
                        return Response(data, status=500, mimetype='application/json')

                else:
                    # return 404: no email for recipient email address
                    resp = {"Error": "Unable to resolve the recipient email address..."}
                    data = json.dumps(resp)
                    return Response(data, status=404, mimetype='application/json')

            # database exception
            except exc.SQLAlchemyError as err:
                resp = {"Database Error": str(err)}
                data = json.dumps(resp)
                return Response(data, status=500, mimetype='application/json')

        # signature and token verification failed
        else:
            resp = {"Signature": form_data['signature'], "Token": form_data['token']}
            data = json.dumps(resp)
            return Response(data, status=409, mimetype='application/json')

    else:
        # method not allowed
        resp = {"Message": "Method Not Allowed"}
        data = json.dumps(resp)
        return Response(data, status=405, mimetype='application/json')


@app.route('/api/v1.0/webhooks/mailgun/lead/open', methods=['POST'])
def lead_opens():
    """
    The lead email opens webhook
    :return: json
    """
    if request.method == 'POST':

        form_data = {
            "message_id": request.form.get('Message-Id', None),
            "x_mail_gun_sid": request.form.get('X-Mailgun-Sid', None),
            "domain": request.form.get('domain', None),
            "event": request.form.get('event', 'open'),
            "timestamp": request.form.get('timestamp', None),
            "recipient": request.form.get('recipient', None),
            "signature": request.form.get('signature', None),
            "token": request.form.get('token', None),
            "ip": request.form.get('ip', None),
            "device_type": request.form.get('device-type', None),
            "client_type": request.form.get('client-type', None),
            "campaign_name": request.form.get('campaign-name', None)
        }

        # verify the mailgun token and signature with the api_key
        token = form_data['token'].encode('utf-8')
        timestamp = form_data['timestamp'].encode('utf-8')
        signature = form_data['signature'].encode('utf-8')
        mg_recipient = form_data['recipient']
        event = form_data['event']
        ip_addr = form_data['ip']
        device_type = form_data['device_type']
        campaign_name = form_data['campaign_name']
        client_type = form_data['client_type']

        if verify(mailgun_api_key, token, timestamp, signature):

            try:
                av = db_session.query(AppendedVisitor).filter(
                    AppendedVisitor.email == mg_recipient
                ).first()

                if av:

                    try:
                        lead = db_session.query(Lead).filter(
                            Lead.appended_visitor_id == av.id
                        ).one()

                        if lead:
                            email = av.email
                            av_id = lead.appended_visitor_id
                            event = form_data['event']

                            # set the dropped flags in the database
                            lead.followup_email_delivered = 0
                            lead.followup_email_status = event
                            lead.followup_email_opens += lead.followup_email_opens
                            lead.followup_email_open_ip = ip_addr
                            lead.followup_email_open_campaign = campaign_name
                            lead.followup_email_open_device = device_type
                            lead.webhook_last_update = datetime.now()
                            db_session.commit()

                            # return a successful response
                            return jsonify({"v_id": av_id, "email": email, "event": event, "status": 'success'}), 202

                        # return 404 for lead not found
                        else:
                            resp = {"Error": "Lead not found..."}
                            data = json.dumps(resp)
                            return Response(data, status=404, mimetype='application/json')

                    # database exception
                    except exc.SQLAlchemyError as err:
                        resp = {"Database Error": str(err)}
                        data = json.dumps(resp)
                        return Response(data, status=500, mimetype='application/json')

                else:
                    # return 404: no email for recipient email address
                    resp = {"Error": "Unable to resolve the recipient email address..."}
                    data = json.dumps(resp)
                    return Response(data, status=404, mimetype='application/json')

            # database exception
            except exc.SQLAlchemyError as err:
                resp = {"Database Error": str(err)}
                data = json.dumps(resp)
                return Response(data, status=500, mimetype='application/json')

        # signature and token verification failed
        else:
            resp = {"Signature": form_data['signature'], "Token": form_data['token']}
            data = json.dumps(resp)
            return Response(data, status=409, mimetype='application/json')

    else:
        # method not allowed
        resp = {"Message": "Method Not Allowed"}
        data = json.dumps(resp)
        return Response(data, status=405, mimetype='application/json')


@app.route('/api/v1.0/auth/login', methods=['GET'])
def login():
    """
    Template for Login page
    :return:
    """
    return render_template(
        'login.html',
        today=get_date()
    )


def send_alerts():
    """
    Send alerts when the EARL Dashboard does not update correctly.
    :return: twilio sid
    """
    admins = config.ADMINS
    client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
    messages = []

    for admin in admins:

        # create and send the sms message to the admins
        msg = client.messages.create(
            to=admin,
            from_="+14152342025",
            body="Warning! EARL Dashboard shows duplicate entries across 2 cycles. "
                 "Check EARL automation ASAP!")

        messages.append(msg.sid)

    # return the message sid
    return messages


def compare_(a, b):
    return a == b


@app.errorhandler(404)
def page_not_found(err):
    return render_template('error-404.html'), 404


@app.errorhandler(500)
def internal_server_error(err):
    return render_template('error-500.html'), 500


def flash_errors(form):
    for field, errors in form.errors.items():
        for error in errors:
            flash(u"Error in the %s field - %s" % (
                getattr(form, field).label.text,
                error
            ))


def send_email(to, subject, msg_body, **kwargs):
    """
    Send Mail function
    :param to:
    :param subject:
    :param template:
    :param kwargs:
    :return: celery async task id
    """
    msg = Message(
        subject,
        sender=app.config['MAIL_DEFAULT_SENDER'],
        recipients=[to, ]
    )
    msg.body = "EARL API v1.0"
    msg.html = msg_body
    mail.send(msg)


def get_date():
    # set the current date time for each page
    today = datetime.now().strftime('%c')
    return '{}'.format(today)


def verify(api_key, token, timestamp, signature):
    hmac_digest = hmac.new(key=mailgun_api_key,
                           msg='{}{}'.format(timestamp, token).encode('utf-8'),
                           digestmod=hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, hmac_digest)


if __name__ == '__main__':
    port = 5880

    # start the application
    app.run(
        debug=debug,
        port=port
    )
