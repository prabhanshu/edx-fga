"""
This block defines a Freeform Graded Assignment.  Students are shown a rubric
and invited to upload a file which is then graded by staff.
"""
import datetime
import hashlib
import json
import logging
import mimetypes
import os
import pkg_resources
import pytz

from functools import partial

from courseware.models import StudentModule

from django.core.exceptions import PermissionDenied
from django.core.files import File
from django.core.files.storage import default_storage
from django.conf import settings
from django.template import Context, Template

from student.models import user_by_anonymous_id
from submissions import api as submissions_api
from submissions.models import StudentItem as SubmissionsStudent

from webob.response import Response

from xblock.core import XBlock
from xblock.exceptions import JsonHandlerError
from xblock.fields import DateTime, Scope, String, Float, Integer
from xblock.fragment import Fragment

from xmodule.util.duedate import get_extended_due_date


log = logging.getLogger(__name__)
BLOCK_SIZE = 8 * 1024


def reify(meth):
    """
    Decorator which caches value so it is only computed once.
    Keyword arguments:
    inst
    """
    def getter(inst):
        """
        Set value to meth name in dict and returns value.
        """
        value = meth(inst)
        inst.__dict__[meth.__name__] = value
        return value
    return property(getter)


class FreeformGradedAssignmentXBlock(XBlock):
    """
    This block defines a Freeform Graded Assignment.  Students are shown a rubric
    and invited to upload a file which is then graded by staff.
    """
    has_score = True
    icon_class = 'problem'
    STUDENT_FILEUPLOAD_MAX_SIZE = 4 * 1000 * 1000  # 4 MB

    display_name = String(
        default='Freeform Graded Assignment', scope=Scope.settings,
        help="This name appears in the horizontal navigation at the top of "
             "the page."
    )

    weight = Float(
        display_name="Problem Weight",
        help=("Defines the number of points each problem is worth. "
              "If the value is not set, the problem is worth the sum of the "
              "option point values."),
        values={"min": 0, "step": .1},
        scope=Scope.settings
    )

    points = Integer(
        display_name="Maximum score",
        help=("Maximum grade score given to assignment by staff."),
        default=100,
        scope=Scope.settings
    )

    staff_score = Integer(
        display_name="Score assigned by non-instructor staff",
        help=("Score will need to be approved by instructor before being "
              "published."),
        default=None,
        scope=Scope.settings
    )

    comment = String(
        display_name="Instructor comment",
        default='',
        scope=Scope.user_state,
        help="Feedback given to student by instructor."
    )

    auto_grade_min_word_count = Integer(
        display_name="Minimum word count",
        help=("Minimum word count to get answer auto-graded by staff."),
        default=5,
        scope=Scope.settings
    )

    auto_grade_max_word_count = Integer(
        display_name="Maximum word count",
        help=("Maximum word count to get answer auto-graded by staff."),
        default=100,
        scope=Scope.settings
    )

    def max_score(self):
        """
        Return the maximum score possible.
        """
        return self.points

    @reify
    def block_id(self):
        """
        Return the usage_id of the block.
        """
        return self.scope_ids.usage_id

    def student_submission_id(self, submission_id=None):
        # pylint: disable=no-member
        """
        Returns dict required by the submissions app for creating and
        retrieving submissions for a particular student.
        """
        if submission_id is None:
            submission_id = self.xmodule_runtime.anonymous_student_id
            assert submission_id != (
                'MOCK', "Forgot to call 'personalize' in test."
            )
        return {
            "student_id": submission_id,
            "course_id": self.course_id,
            "item_id": self.block_id,
            "item_type": 'fga',  # ???
        }

    def get_submission(self, submission_id=None):
        """
        Get student's most recent submission.
        """
        submissions = submissions_api.get_submissions(
            self.student_submission_id(submission_id))
        if submissions:
            # If I understand docs correctly, most recent submission should
            # be first
            return submissions[0]

    def get_score(self, submission_id=None):
        """
        Return student's current score.
        """
        score = submissions_api.get_score(
            self.student_submission_id(submission_id)
        )
        if score:
            return score['points_earned']

    @reify
    def score(self):
        """
        Return score from submissions.
        """
        return self.get_score()

    def student_view(self, context=None):
        # pylint: disable=no-member
        """
        The primary view of the FreeformGradedAssignmentXBlock, shown to students
        when viewing courses.
        """
        context = {
            "student_state": json.dumps(self.student_state()),
            "id": self.location.name.replace('.', '_')
        }
        if self.show_staff_grading_interface():
            context['is_course_staff'] = True
            self.update_staff_debug_context(context)

        fragment = Fragment()
        fragment.add_content(
            render_template(
                'templates/freeform_graded_assignment/show.html',
                context
            )
        )
        fragment.add_css(_resource("static/css/edx_fga.css"))
        fragment.add_javascript(_resource("static/js/src/edx_fga.js"))
        fragment.add_javascript(_resource("static/js/src/jquery.tablesorter.min.js"))
        fragment.initialize_js('FreeformGradedAssignmentXBlock')
        return fragment

    def update_staff_debug_context(self, context):
        # pylint: disable=no-member
        """
        Add context info for the Staff Debug interface.
        """
        published = self.start
        context['is_released'] = published and published < _now()
        context['location'] = self.location
        context['category'] = type(self).__name__
        context['fields'] = [
            (name, field.read_from(self))
            for name, field in self.fields.items()]

    def student_state(self):
        """
        Returns a JSON serializable representation of student's state for
        rendering in client view.
        """
        submission = self.get_submission()
        if submission:
            freeform_answer = {"freeform_answer": submission['answer']['freeform_answer']}
        else:
            freeform_answer = None

        score = self.score
        if score is not None:
            graded = {'score': score, 'comment': self.comment}
        else:
            graded = None

        return {
            "display_name": self.display_name,
            "freeform_answer": freeform_answer,
            # "annotated": annotated,
            "graded": graded,
            "max_score": self.max_score(),
            "answer_allowed": self.answer_allowed(),
        }

    def staff_grading_data(self):
        """
        Return student assignment information for display on the
        grading screen.
        """
        def get_student_data():
            # pylint: disable=no-member
            """
            Returns a dict of student assignment information along with
            annotated file name, student id and module id, this
            information will be used on grading screen
            """
            # Submissions doesn't have API for this, just use model directly.
            students = SubmissionsStudent.objects.filter(
                course_id=self.course_id,
                item_id=self.block_id)
            for student in students:
                submission = self.get_submission(student.student_id)
                if not submission:
                    continue
                user = user_by_anonymous_id(student.student_id)
                module, created = StudentModule.objects.get_or_create(
                    course_id=self.course_id,
                    module_state_key=self.location,
                    student=user,
                    defaults={
                        'state': '{}',
                        'module_type': self.category,
                    })
                if created:
                    log.info(
                        "Init for course:%s module:%s student:%s  ",
                        module.course_id,
                        module.module_state_key,
                        module.student.username
                    )

                state = json.loads(module.state)
                score = self.get_score(student.student_id)
                approved = score is not None
                if score is None:
                    score = state.get('staff_score')
                    needs_approval = score is not None
                else:
                    needs_approval = False
                instructor = self.is_instructor()
                yield {
                    'module_id': module.id,
                    'student_id': student.student_id,
                    'submission_id': submission['uuid'],
                    'username': module.student.username,
                    'fullname': module.student.profile.name,
                    'freeform_answer': submission['answer']['freeform_answer'],
                    'timestamp': submission['created_at'].strftime(
                        DateTime.DATETIME_FORMAT
                    ),
                    'score': score,
                    'approved': approved,
                    'needs_approval': instructor and needs_approval,
                    'may_grade': instructor or not approved,
                    # 'annotated': state.get("annotated_filename"),
                    'comment': state.get("comment", ''),
                }

        return {
            'assignments': list(get_student_data()),
            'max_score': self.max_score(),
            'display_name': self.display_name
        }

    def studio_view(self, context=None):
        """
        Return fragment for editing block in studio.
        """
        try:
            cls = type(self)

            def none_to_empty(data):
                """
                Return empty string if data is None else return data.
                """
                return data if data is not None else ''
            edit_fields = (
                (field, none_to_empty(getattr(self, field.name)), validator)
                for field, validator in (
                    (cls.display_name, 'string'),
                    (cls.points, 'number'),
                    (cls.weight, 'number'),
                    (cls.auto_grade_min_word_count, 'number'),
                    (cls.auto_grade_max_word_count, 'number'))
            )

            context = {
                'fields': edit_fields
            }
            fragment = Fragment()
            fragment.add_content(
                render_template(
                    'templates/freeform_graded_assignment/edit.html',
                    context
                )
            )
            fragment.add_javascript(_resource("static/js/src/studio.js"))
            fragment.initialize_js('FreeformGradedAssignmentXBlock')
            return fragment
        except:  # pragma: NO COVER
            log.error("Don't swallow my exceptions", exc_info=True)
            raise

    @XBlock.json_handler
    def save_fga(self, data, suffix=''):
        # pylint: disable=unused-argument
        """
        Persist block data when updating settings in studio.
        """
        self.display_name = data.get('display_name', self.display_name)

        # Validate points before saving
        points = data.get('points', self.points)
        # Check that we are an int
        try:
            points = int(points)
        except ValueError:
            raise JsonHandlerError(400, 'Points must be an integer')
        # Check that we are positive
        if points < 0:
            raise JsonHandlerError(400, 'Points must be a positive integer')
        self.points = points

        # Validate weight before saving
        weight = data.get('weight', self.weight)
        # Check that weight is a float.
        if weight:
            try:
                weight = float(weight)
            except ValueError:
                raise JsonHandlerError(400, 'Weight must be a decimal number')
            # Check that we are positive
            if weight < 0:
                raise JsonHandlerError(
                    400, 'Weight must be a positive decimal number'
                )
        self.weight = weight

        # Validate weight before saving
        auto_grade_min_word_count = data.get('auto_grade_min_word_count', self.auto_grade_min_word_count)
        # Check that weight is a float.
        if auto_grade_min_word_count:
            try:
                auto_grade_min_word_count = int(auto_grade_min_word_count)
            except ValueError:
                raise JsonHandlerError(400, 'Minimum word count must be an integer')
            # Check that we are positive
            if auto_grade_min_word_count < 0:
                raise JsonHandlerError(
                    400, 'Minimum word count must be a positive integer'
                )
            if auto_grade_min_word_count > 100:
                raise JsonHandlerError(
                    400, 'Minimum word count must be less than 100'
                )
        self.auto_grade_min_word_count = auto_grade_min_word_count

        # Validate weight before saving
        auto_grade_max_word_count = data.get('auto_grade_max_word_count', self.auto_grade_max_word_count)
        # Check that weight is a float.
        if auto_grade_max_word_count:
            try:
                auto_grade_max_word_count = int(auto_grade_max_word_count)
            except ValueError:
                raise JsonHandlerError(400, 'Maximum word count must be an integer')
            # Check that we are positive
            if auto_grade_max_word_count < 0:
                raise JsonHandlerError(
                    400, 'Maximum word count must be a positive integer'
                )
            if auto_grade_max_word_count > 200:
                raise JsonHandlerError(
                    400, 'Maximum word count must be less than 200'
                )
            if auto_grade_max_word_count < auto_grade_min_word_count:
                raise JsonHandlerError(
                    400, 'Maximum word count must be greater than minimum word count'
                )

        self.auto_grade_max_word_count = auto_grade_max_word_count

    @XBlock.handler
    def upload_assignment(self, request, suffix=''):
        # pylint: disable=unused-argument
        """
        Save a students submission file.
        """
        # require(self.upload_allowed())
        # upload = request.params['assignment']
        # sha1 = _get_sha1(upload.file)
        freeform_answer = request.params.get('freeform_answer', None)
        answer = {
            # "sha1": sha1,
            # "filename": upload.file.name,
            # "mimetype": mimetypes.guess_type(upload.file.name)[0],
            "freeform_answer": freeform_answer


        }
        student_id = self.student_submission_id()
        submissions_api.create_submission(student_id, answer)
        # path = self._file_storage_path(sha1, upload.file.name)
        # if not default_storage.exists(path):
        #     default_storage.save(path, File(upload.file))

        return Response(json_body=self.student_state())

    @XBlock.handler
    def get_staff_grading_data(self, request, suffix=''):
        # pylint: disable=unused-argument
        """
        Return the html for the staff grading view
        """
        require(self.is_course_staff())
        return Response(json_body=self.staff_grading_data())

    def validate_score_message(self, course_id, username):
        log.error(
            "enter_grade: invalid grade submitted for course:%s module:%s student:%s",
            course_id,
            self.location,
            username
        )
        return {
            "error": "Please enter valid grade"
        }

    @XBlock.handler
    def enter_grade(self, request, suffix=''):
        # pylint: disable=unused-argument
        """
        Persist a score for a student given by staff.
        """
        require(self.is_course_staff())
        score = request.params.get('grade', None)
        module = StudentModule.objects.get(pk=request.params['module_id'])
        if not score:
            return Response(
                json_body=self.validate_score_message(
                    module.course_id,
                    module.student.username
                )
            )

        state = json.loads(module.state)
        try:
            score = int(score)
        except ValueError:
            return Response(
                json_body=self.validate_score_message(
                    module.course_id,
                    module.student.username
                )
            )

        if self.is_instructor():
            uuid = request.params['submission_id']
            submissions_api.set_score(uuid, score, self.max_score())
        else:
            state['staff_score'] = score
        state['comment'] = request.params.get('comment', '')
        module.state = json.dumps(state)
        module.save()
        log.info(
            "enter_grade for course:%s module:%s student:%s",
            module.course_id,
            module.module_state_key,
            module.student.username
        )

        return Response(json_body=self.staff_grading_data())

    @XBlock.handler
    def remove_grade(self, request, suffix=''):
        # pylint: disable=unused-argument
        """
        Reset a students score request by staff.
        """
        require(self.is_course_staff())
        student_id = request.params['student_id']
        submissions_api.reset_score(student_id, unicode(self.course_id), unicode(self.block_id))
        module = StudentModule.objects.get(pk=request.params['module_id'])
        state = json.loads(module.state)
        state['staff_score'] = None
        state['comment'] = ''
        module.state = json.dumps(state)
        module.save()
        log.info(
            "remove_grade for course:%s module:%s student:%s",
            module.course_id,
            module.module_state_key,
            module.student.username
        )
        return Response(json_body=self.staff_grading_data())

    def is_course_staff(self):
        # pylint: disable=no-member
        """
         Check if user is course staff.
        """
        return getattr(self.xmodule_runtime, 'user_is_staff', False)

    def is_instructor(self):
        # pylint: disable=no-member
        """
        Check if user role is instructor.
        """
        return self.xmodule_runtime.get_user_role() == 'instructor'

    def show_staff_grading_interface(self):
        """
        Return if current user is staff and not in studio.
        """
        in_studio_preview = self.scope_ids.user_id is None
        return self.is_course_staff() and not in_studio_preview

    def past_due(self):
        """
        Return whether due date has passed.
        """
        due = get_extended_due_date(self)
        if due is not None:
            return _now() > due
        return False

    def answer_allowed(self):
        """
        Return whether student is allowed to submit an assignment.
        """
        return not self.past_due() and self.score is None

def _resource(path):  # pragma: NO COVER
    """
    Handy helper for getting resources from our kit.
    """
    data = pkg_resources.resource_string(__name__, path)
    return data.decode("utf8")


def _now():
    """
    Get current date and time.
    """
    return datetime.datetime.utcnow().replace(tzinfo=pytz.utc)


def load_resource(resource_path):  # pragma: NO COVER
    """
    Gets the content of a resource
    """
    resource_content = pkg_resources.resource_string(__name__, resource_path)
    return unicode(resource_content)


def render_template(template_path, context=None):  # pragma: NO COVER
    """
    Evaluate a template by resource path, applying the provided context.
    """
    if context is None:
        context = {}

    template_str = load_resource(template_path)
    template = Template(template_str)
    return template.render(Context(context))


def require(assertion):
    """
    Raises PermissionDenied if assertion is not true.
    """
    if not assertion:
        raise PermissionDenied
