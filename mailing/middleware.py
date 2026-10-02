class DropContentLengthOnNoContent:
    """Remove Content-Length from 204 responses.

    CommonMiddleware adds the header after the view. HTTP/2 rejects it on
    204, which breaks the public signup CORS preflight.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if response.status_code == 204 and response.has_header("Content-Length"):
            del response["Content-Length"]
        return response
