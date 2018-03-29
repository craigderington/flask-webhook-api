#! .env/bin/python
# ** -- coding: utf-8 -- **
from flask import Flask, session, request, redirect, jsonify, abort, escape, make_response, url_for, \
    render_template, flash
from flask_restful import Api, Resource, reqparse, fields, marshal
from flask_httpauth import HTTPBasicAuth
from datetime import datetime
import json
from hashlib import sha384
import config


app = Flask(__name__)
api = Api(app)
auth = HTTPBasicAuth()
app.secret_key = config.SECRET_KEY


@app.route('/api/v1.0/index')
def index():
    if 'username' not in session:
        return redirect(url_for('login'))

    context = {
        "username_session": escape(session['username']).capitalize(),
        "userid": session['userid']
    }

    return render_template('index.html', context=context)


@app.route('/api/v1.0/login', methods=['GET', 'POST'])
def login():
    error = None

    if 'username' in session:
        return redirect(url_for('index'))

    # the code below is executed if the request method
    # was GET or the credentials were invalid
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.pop('username', None)
    session.pop('userid', None)
    return redirect(url_for('index'))


@app.route('/forgot')
def forgot_password():
    return render_template('forgot.html', username=None)


@auth.get_password
def get_password_and_key(username):
    """ Simple text-based authentication """
    if username == '':
        api_key = ''
        return api_key
    else:
        return None


@auth.error_handler
def unauthorized():
    """
    Return a 403 instead of a 401 to prevent browsers from displaying
    the default auth dialog
    :param: none
    :return: unauthorized message
    """
    return make_response(jsonify({'message': 'Unauthorized Access'}), 403)


marshall_fields = {}


class VisitorListAPI(Resource):
    """
    API Resource for listing visitors from the database
    Provides the endpoint for creating new visitors
    :param: none
    :type a json object
    :return json stats list for all players
    """
    decorators = [auth.login_required]

    def __init__(self):
        self.reqparse = reqparse.RequestParser()
        self.reqparse.add_argument('id', type=int, required=False,
                                   help='The API URL\'s ID of the Visitor.')

        super(VisitorListAPI, self).__init__()

    def get(self):
        try:
            sql = ""
            conn = ""
            params = 'this'
            cursor = conn.query(sql, params)
            columns = [column[0] for column in cursor.description]
            visitors = []
            for row in cursor.fetchall():
                visitors.append(dict(zip(columns, row)))

            return {
                'visitors': marshal(visitors, marshall_fields)
            }

        except Exception as e:
            return {'error': str(e)}

    def post(self):
        try:
            args = self.reqparse.parse_args()
            data = request.get_json()

            visitor = {}

            conn = ""
            conn.query()
            conn.commit()

            return {
                'visitor': visitor
            }, 201

        except Exception as e:
            return {'error': str(e)}


class VisitorAPI(Resource):
    """
    API Resource for retrieving, modifying, updating and deleting a single
    visitor object
    :param: id
    :return: json
    """
    decorators = [auth.login_required]

    def __init__(self):
        self.reqparse = reqparse.RequestParser()
        self.reqparse.add_argument('id', type=int, required=False,
                                   help='The API URL\'s ID of the stat.')
        super(VisitorAPI, self).__init__()

    def get(self, id):
        try:
            conn = ""
            params = id
            sql = ""

            cursor = conn.query(sql, params)
            columns = [column[0] for column in cursor.description]
            visitor = []
            for row in cursor.fetchall():
                visitor.append(dict(zip(columns, row)))

            return {
                'visitor': marshal(visitor, marshall_fields)
            }, 200

        except Exception as e:
            return {'error': str(e)}

    def put(self, id):
        try:
            conn = ""
            data = request.get_json()
            params = ()
            conn.query()

            conn.commit()

            return {
                'visitor': data
            }, 204

        except Exception as e:
            return {'error': str(e)}

    def delete(self, id):
        try:
            conn = ""
            params = id
            sql = ""
            cursor = conn.query(sql, params)
            conn.commit()

            return {
                'result': True
            }, 204

        except Exception as e:
            return {'error': str(e)}


# register the API resources and define endpoints
api.add_resource(VisitorListAPI, '/api/v1.0/visitors/', endpoint='visitors')
api.add_resource(VisitorAPI, '/api/v1.0/visitor/<int:id>', endpoint='visitor')

if __name__ == '__main__':
    app.run(
        debug=config.DEBUG,
        port=config.PORT
    )
