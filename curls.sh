#!/usr/bin/env bash
set -euo pipefail

# -----------------------------------------------------------------------------
# CONFIGURATION & COLOR SETUP
# -----------------------------------------------------------------------------
BASE_URL="http://localhost:8080"
USERNAME="admin"
PASSWORD="admin"
PAYLOAD="hello world!"
PAYLOAD_LEN=${#PAYLOAD}
FILENAME_BASE64=$(echo -n "test.txt" | base64)

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m' # No Color

log_step() { echo -e "\n${GREEN}[STEP] $1${NC}"; }
fail() { echo -e "${RED}[ERROR] $1${NC}"; exit 1; }

# -----------------------------------------------------------------------------
# STEP 1: AUTHENTICATE & EXTRACT TOKEN + USER_ID
# -----------------------------------------------------------------------------
log_step "Authenticating with API..."

LOGIN_RESP=$(curl -s -f -X POST "$BASE_URL/public/login" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=$USERNAME&password=$PASSWORD") || fail "Login failed. Check server or credentials."

TOKEN=$(echo "$LOGIN_RESP" | jq -r '.access_token')
if [ -z "$TOKEN" ] || [ "$TOKEN" == "null" ]; then
  fail "Failed to retrieve access token."
fi

# Extract User ID from JWT payload
USER_ID=$(echo "$TOKEN" | cut -d. -f2 | base64 --decode 2>/dev/null | jq -r '.sub')
echo "Authenticated as User ID: $USER_ID"

# -----------------------------------------------------------------------------
# STEP 2: VERIFY ADMIN & MEDIA ENDPOINTS
# -----------------------------------------------------------------------------
log_step "Checking GET /api/v1/admin/users..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X GET "$BASE_URL/api/v1/admin/users" \
  -H "Authorization: Bearer $TOKEN")
[ "$HTTP_CODE" -eq 200 ] || fail "Admin check failed with HTTP status $HTTP_CODE"

log_step "Checking GET /api/v1/$USER_ID/media..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X GET "$BASE_URL/api/v1/$USER_ID/media" \
  -H "Authorization: Bearer $TOKEN")
[ "$HTTP_CODE" -eq 200 ] || fail "List media failed with HTTP status $HTTP_CODE"

# -----------------------------------------------------------------------------
# STEP 3: CREATE TUS UPLOAD SESSION (POST)
# -----------------------------------------------------------------------------
log_step "Creating tus upload session..."

HEADERS_FILE=$(mktemp)
HTTP_CODE=$(curl -s -o /dev/null -D "$HEADERS_FILE" -w "%{http_code}" -X POST "$BASE_URL/api/v1/$USER_ID/media" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Tus-Resumable: 1.0.0" \
  -H "Upload-Length: $PAYLOAD_LEN" \
  -H "Upload-Metadata: filename $FILENAME_BASE64")

[ "$HTTP_CODE" -eq 201 ] || fail "Failed to create upload session. HTTP status: $HTTP_CODE"

LOCATION_HEADER=$(grep -i "^location:" "$HEADERS_FILE" | tr -d '\r' | awk '{print $2}')
SESSION_ID="${LOCATION_HEADER##*/}"
rm -f "$HEADERS_FILE"

[ -n "$SESSION_ID" ] || fail "Location header missing in session response."
echo "Created Upload Session ID: $SESSION_ID"

# -----------------------------------------------------------------------------
# STEP 4: VERIFY OFFSET (HEAD)
# -----------------------------------------------------------------------------
log_step "Checking upload offset via HEAD..."

HEADERS_FILE=$(mktemp)
HTTP_CODE=$(curl -s -o /dev/null -D "$HEADERS_FILE" -w "%{http_code}" -X HEAD "$BASE_URL/api/v1/$USER_ID/media/$SESSION_ID" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Tus-Resumable: 1.0.0")

[ "$HTTP_CODE" -eq 200 ] || fail "HEAD request failed. HTTP status: $HTTP_CODE"

OFFSET=$(grep -i "^upload-offset:" "$HEADERS_FILE" | tr -d '\r' | awk '{print $2}')
rm -f "$HEADERS_FILE"
echo "Current Upload Offset: $OFFSET"

# -----------------------------------------------------------------------------
# STEP 5: STREAM CHUNK DATA (PATCH)
# -----------------------------------------------------------------------------
log_step "Streaming binary payload via PATCH..."

HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X PATCH "$BASE_URL/api/v1/$USER_ID/media/$SESSION_ID" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Tus-Resumable: 1.0.0" \
  -H "Content-Type: application/offset+octet-stream" \
  -H "Upload-Offset: $OFFSET" \
  --data-binary "$PAYLOAD")

[ "$HTTP_CODE" -eq 204 ] || fail "PATCH upload failed. HTTP status: $HTTP_CODE"

# -----------------------------------------------------------------------------
# STEP 6: VERIFY COMPLETED FILE & DELETE
# -----------------------------------------------------------------------------
log_step "Retrieving completed file ID..."

MEDIA_LIST=$(curl -s -X GET "$BASE_URL/api/v1/$USER_ID/media" \
  -H "Authorization: Bearer $TOKEN")

FILE_ID=$(echo "$MEDIA_LIST" | jq -r '.files[0].id')
[ -n "$FILE_ID" ] && [ "$FILE_ID" != "null" ] || fail "Could not locate completed file ID."

echo "Completed File ID: $FILE_ID"

log_step "Deleting file $FILE_ID..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE "$BASE_URL/api/v1/$USER_ID/media/files/$FILE_ID" \
  -H "Authorization: Bearer $TOKEN")

[ "$HTTP_CODE" -eq 200 ] || fail "Delete file failed. HTTP status: $HTTP_CODE"

echo -e "\n${GREEN}All automated tests passed successfully!${NC}"