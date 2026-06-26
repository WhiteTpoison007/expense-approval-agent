import sys
from unittest.mock import MagicMock
import google.auth
from google.auth.credentials import Credentials

# Mock google.auth.default to prevent DefaultCredentialsError during test collection
mock_credentials = MagicMock(spec=Credentials)
google.auth.default = lambda *args, **kwargs: (mock_credentials, "dummy-project")
