import http.server
import socketserver
import json
import os

# --- Configuration ---
# The directory where your JSON files will be stored.
# This directory will be created if it doesn't exist.
JSON_FILES_DIR = "nodes"
# The port on which the server will listen.
PORT = 8082

# --- Custom Request Handler ---
class JSONFileHandler(http.server.SimpleHTTPRequestHandler):
    """
    A custom HTTP request handler that serves JSON files from a specific directory.
    It overrides the do_GET method to handle requests for JSON files.
    """

    def do_GET(self):
        """
        Handles GET requests.
        It parses the request path, locates the corresponding JSON file,
        reads its content, and sends it as a JSON response.
        """
        # Remove the leading slash from the requested path (e.g., "/nodes/file.json" becomes "nodes/file.json")
        requested_path = self.path.lstrip('/')

        # --- Security Check 1: Ensure request is for the designated JSON_FILES_DIR ---
        # This prevents requests like "/some_other_dir/file.txt"
        if not requested_path.startswith(JSON_FILES_DIR + '/'):
            self.send_error(403, "Forbidden: Only access to files within the '/nodes/' path is allowed.")
            return

        # Extract the file name from the requested path (e.g., "file.json" from "nodes/file.json")
        file_name = requested_path[len(JSON_FILES_DIR) + 1:]

        # Construct the absolute base directory path for security
        # This ensures that any file access attempts are strictly confined to 'JSON_FILES_DIR'
        base_dir_abs = os.path.abspath(os.path.join(os.getcwd(), JSON_FILES_DIR))

        # Construct the absolute path to the requested file
        # os.path.join handles path segments correctly, including on different OS.
        file_path_abs = os.path.abspath(os.path.join(base_dir_abs, file_name))

        # --- Security Check 2: Prevent directory traversal attacks ---
        # This check ensures that the resolved file_path_abs is indeed a child of base_dir_abs.
        # For example, if file_name was "../secret.txt", file_path_abs would resolve outside base_dir_abs.
        if not file_path_abs.startswith(base_dir_abs + os.sep):
            self.send_error(403, "Forbidden: Attempted directory traversal detected.")
            return

        # Check if the file exists and is a regular file (not a directory)
        if os.path.exists(file_path_abs) and os.path.isfile(file_path_abs):
            try:
                # Open and read the file content
                with open(file_path_abs, 'r', encoding='utf-8') as f:
                    content = f.read()

                # Attempt to parse the content as JSON
                json_data = json.loads(content)

                # --- Send Successful Response ---
                self.send_response(200) # HTTP 200 OK
                self.send_header("Content-type", "application/json") # Set content type to JSON
                self.end_headers()

                # Send the JSON data, pretty-printed for readability, and encoded to bytes
                self.wfile.write(json.dumps(json_data, indent=2).encode('utf-8'))

            except json.JSONDecodeError:
                # Handle cases where the file content is not valid JSON
                self.send_error(500, "Internal Server Error: The file content is not valid JSON.")
            except Exception as e:
                # Catch any other unexpected errors during file reading or processing
                self.send_error(500, f"Internal Server Error: Could not process file. Details: {e}")
        else:
            # If the file does not exist or is not a regular file
            self.send_error(404, "Not Found: The requested JSON file was not found in the 'nodes' directory.")

# --- Server Setup and Run Function ---
def run_server():
    """
    Sets up and starts the HTTP server.
    It also ensures the 'nodes' directory exists.
    """
    # Create the JSON_FILES_DIR if it doesn't already exist
    if not os.path.exists(JSON_FILES_DIR):
        os.makedirs(JSON_FILES_DIR)
        print(f"Server: Created directory '{JSON_FILES_DIR}' for JSON files.")

    # Use socketserver.TCPServer to create a TCP/IP server
    # The first argument is the address (empty string means all available interfaces)
    # The second argument is our custom request handler
    with socketserver.TCPServer(("", PORT), JSONFileHandler) as httpd:
        print(f"Server: Serving JSON files from './{JSON_FILES_DIR}/' at http://localhost:{PORT}")
        print(f"Server: To access a file, use a URL like: http://localhost:{PORT}/nodes/your_file_name.json")
        print("Server: Press Ctrl+C to stop the server.")
        try:
            # Start the server and keep it running indefinitely
            httpd.serve_forever()
        except KeyboardInterrupt:
            # Handle Ctrl+C to gracefully shut down the server
            print("\nServer: Shutting down the server.")
            httpd.shutdown() # Cleanly shuts down the server

# --- Main Execution Block ---
if __name__ == "__main__":
    run_server()
