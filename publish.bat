@echo off
rem Publish the next due Approved carousel. Double-click to run.
rem Reads IG_ACCESS_TOKEN and IG_BUSINESS_ACCOUNT_ID from your user environment variables.
cd /d "%~dp0"
python ".claude\skills\infographic-generator\scripts\publish_post.py" %*
echo.
pause
