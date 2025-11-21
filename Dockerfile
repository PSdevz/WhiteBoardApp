# Step 1: Start with a base that already has Python
# Using "slim" is good practice as it's smaller.
FROM python:3.9-slim

# Step 2: Set the "working directory" inside the container.
# All our commands will run from here.
WORKDIR /app

# Step 3: Copy the requirements file FIRST and install packages
# This is an optimization. Docker caches this step, so if you
# only change your app code, the build is much faster.
COPY requirements .
RUN pip install --no-cache-dir -r requirements

# Step 4: Copy the rest of your app code into the /app directory
# This copies app.py, the Dockerfile, etc.
# It also copies the "templates" folder (with index.html).
COPY . .

# Step 5: Tell Docker what port your app runs on
# This must match the port in your app.py (5001).
EXPOSE 5001

# Step 6: The command to run your app when the container starts.
# We use the eventlet server for production.
CMD ["python", "-u", "app.py"]