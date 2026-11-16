FACULTY DEMO UI PATCH

Requires previous cn_auto_mode_patch.zip applied (Auto mode enabled).

1. Stop dashboard using Ctrl+C.
2. From project directory: cp ui/app.py ui/app_before_faculty.py; cp ui/static/index.html ui/static/index_before_faculty.html
3. unzip -o ~/Downloads/cn_faculty_dashboard_patch.zip
4. python3 -m py_compile ui/app.py
5. python3 -m unittest discover -s tests -p "test_policy.py" -v
6. python3 -m unittest discover -s tests -p "test_auto_switch.py" -v
7. Check 6653 is free and run sudo python3 ui/app.py
8. Visit http://localhost:8080

Security: dashboard binds to localhost and runs as root. Do not expose this port externally.
Experiment note: automated switching is at new TCP connection boundaries. RR baseline ignores health intentionally; comparison may show failures. Live metrics are not fabricated.
