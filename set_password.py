"""
Run this script to set secure passwords for your users.
Usage: python set_password.py

It will print the hashed password value — copy it into config.py.
"""
import getpass
from werkzeug.security import generate_password_hash

print("Available users: salesdesk, admin")
username = input("Username to set password for: ").strip()
password = getpass.getpass("New password: ")
confirm  = getpass.getpass("Confirm password: ")

if password != confirm:
    print("Passwords do not match.")
else:
    hashed = generate_password_hash(password)
    print(f'\nIn config.py, update the password for "{username}" to:')
    print(f'  "{hashed}"')
