from waitress import serve
from main import app
from dotenv import load_dotenv
import os
load_dotenv()

if __name__ == '__main__':
    print('server running')
    serve(app, host=os.getenv('HOST_NAME'), port=os.getenv('PORT'))