## Testing Explanation/Description

### How to Run

Start up the environment with `make down up`, wait for the service to be running, and run `make test`.
This Makefile target with exec into the `pods-api` pod and run pytest with the files in `/home/tapis/tests`.

The code ran is:

```
pytest --maxfail 1 tests/tests_base.py --disable-pytest-warnings --durations=0
```
 - `--maxfail 1`: pytest will end after X failure(s), useful for debugging imo.

 - `tests/tests_base.py`: This is the path to the Python file containing the tests you want to run. Pytest will discover and run all the tests in this file.

 - `--disable-pytest-warnings`: Without this the output is very cluttered.

 - `--durations=0`: List all tests sorted by how long they took. The 0 argument means that pytest should list the durations of all tests, not just the slowest.



### File Order
We intend for all the test files to be self-contained are they can be ran in any order. The `tests_base.py` file in particular should be ran first however. This test file runs a basic diagnostic on pods and volumes to get a quick overview of the Pods Services' status.


### Objects from Tests
FYI. The tests create objects by default in the dev tenant of which ever environment it's in. The tests create decently uniquely named objects, but there might be potential collision, so be aware. The creation and deletion of objects, so Pods, Volumes, and Snapshots, are taken care of by `pytest.fixture` functions at the top of each test file. These fixtures create the object, `yield` for the duration of the testing, and then delete the objects when the tests are done or the tests raise an error.