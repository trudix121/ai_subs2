from waitress import serve
from main import app
from dotenv import load_dotenv
import os
load_dotenv()

if __name__ == '__main__':
    serve(app, host="0.0.0.0", port=os.getenv('PORT'))