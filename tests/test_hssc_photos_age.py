import hashlib
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image
from bise_results.demographics import apply_age_policy, estimated_age
from bise_results.parser import parse_result, ReviewRequired
from bise_results.photos import photo_url, inspect_photo, save_photo, export_photos
from bise_results.store import locked_store, import_batch, jobs_for, retain_in_database, prepare_jobs
from bise_results.retention import discard_verified_capture

FIXTURE=Path(__file__).parent/'fixtures/hssc-part2.html'


class PhotosAndAgeTests(unittest.TestCase):
    def parsed(self):
        parsed=parse_result(FIXTURE.read_text(),'500001',2024,course='HSSC')
        apply_age_policy(parsed,'estimate',18)
        return parsed

    def test_estimate_never_becomes_dob_or_eligibility(self):
        parsed=self.parsed()
        self.assertEqual(parsed['date_of_birth'],'')
        self.assertEqual(estimated_age(parsed,2026),20)
        self.assertFalse(parsed['sports_registration_ready'])
        self.assertFalse(parsed['age_estimate']['individual_verified'])
        with self.assertRaises(ReviewRequired):apply_age_policy(parse_result(FIXTURE.read_text(),'500001',2024,course='HSSC'))

    def test_verified_dob_is_preserved(self):
        parsed=self.parsed();parsed['date_of_birth']='09/05/2006';parsed.pop('age_estimate')
        apply_age_policy(parsed,'estimate',18)
        self.assertEqual(parsed['date_of_birth'],'09/05/2006')
        self.assertNotIn('age_estimate',parsed)

    def test_only_actual_student_photo_endpoint_is_accepted(self):
        for src in ('https://evil.example/ImageLoader.aspx?x=1','http://127.0.0.1/ImageLoader.aspx?x=1',
                    'Images/mono.jpg','javascript:alert(1)','//result.biselahore.com@evil.example/a'):
            with self.assertRaises(ValueError):photo_url(f'<img id="imgDisplay" src="{src}">','http://result.biselahore.com/ResultPart2.aspx')
        with self.assertRaises(ValueError):photo_url('<img id="ImgBoard" src="Images/mono.jpg">','http://result.biselahore.com/')
        self.assertIn('/ImageLoader.aspx?',photo_url('<img id="imgDisplay" src="ImageLoader.aspx?Updateid=path/PIC/a.jpg">','http://result.biselahore.com/ResultPart2.aspx'))

    def test_photo_cleanup_resume_export_and_hash_guard(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);csv=root/'rolls.csv';csv.write_text('Roll Number\n500001\n')
            folder=root/'job-1';folder.mkdir()
            image=BytesIO();Image.new('RGB',(120,130),'blue').save(image,format='JPEG');payload=image.getvalue()
            parsed=self.parsed();parsed['student_photo']={**inspect_photo(payload),'source_url':'http://result.biselahore.com/ImageLoader.aspx?Updateid=sample/PIC/1.jpg'}
            (folder/'student-photo.jpg').write_bytes(payload)
            with locked_store(root/'results.sqlite3') as db:
                batch=import_batch(db,csv,'Pilot',2024,course='HSSC');job=jobs_for(db,batch)[0]
                with db:
                    save_photo(db,job['id'],parsed,folder)
                    db.execute("UPDATE jobs SET state='verified',result_json=?,receipt_json='{}'",(json.dumps(parsed),))
                discard_verified_capture(db,job['id'],folder)
                self.assertFalse(folder.exists())
                self.assertEqual(prepare_jobs(db,batch,True),[])
                out=root/'Photos';self.assertEqual(export_photos(db,out)['written'],1)
                self.assertEqual(export_photos(db,out)['written'],0)
                exported=out/'2024/HSSC/Part-2/500001.jpg'
                self.assertEqual(hashlib.sha256(exported.read_bytes()).hexdigest(),parsed['student_photo']['sha256'])
                mapping=json.loads((out/'Photo Mapping.json').read_text())[0]
                self.assertEqual(mapping['student_cnic'],parsed['student_cnic'])
                with db:db.execute("UPDATE student_photos SET payload=?",(b'corrupt',))
                with self.assertRaises(ValueError):retain_in_database(db,jobs_for(db,batch)[0],'must preserve captures')

    def test_invented_date_or_estimate_missing_audit_cannot_waive_retention(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);csv=root/'rolls.csv';csv.write_text('Roll Number\n500001\n')
            with locked_store(root/'db') as db:
                batch=import_batch(db,csv,'Pilot',2024,course='HSSC')
                for field in ('age_estimate','dob_status','sports_registration_ready'):
                    parsed=self.parsed();parsed.pop(field)
                    with db:db.execute("UPDATE jobs SET state='verified',result_json=?",(json.dumps(parsed),))
                    with self.assertRaises(ValueError):retain_in_database(db,jobs_for(db,batch)[0],'reject')

    def test_html_or_oversized_image_is_never_accepted_as_photo(self):
        for payload in (b'<html>error</html>',b'',b'x'*(5*1024**2+1)):
            with self.assertRaises((ValueError,OSError)):inspect_photo(payload)


if __name__=='__main__':unittest.main()
