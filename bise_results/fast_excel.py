"""Direct XLSX serialization, preserving report tables, styling and navigation."""
from collections import Counter,defaultdict
from pathlib import Path
import xlsxwriter
from .publishing import unique_names
from .presentation import LABELS,SUBJECT_FIELDS,SUBJECT_LABELS,GREEN,display

WIDTHS={'Roll No':12,'Institution / District':28,'Student Name':27,'Father Name':29,'Date of Birth':16,
        'Student CNIC / B-Form':25,'Father CNIC':23,'Registration No':25,'Board Result / Remarks':60,
        'Board Total Details':45,'Additional Notes':55,'Retrieved At':28}


def build_workbook(path,records,summary,title,columns,safe_text,text_columns,institutions=(),queue_summary=None):
    # Per-student card URLs hit Excel's 65,530-link limit on large masters.
    # Cards remain available as roll-named files; Excel omits this column.
    columns = [column for column in columns if column != 'result_card']
    # Keep tables and merged overview cells; constant_memory disables tables.
    # Direct XLSX writing avoids a full Python XML cell object graph.
    with xlsxwriter.Workbook(path,{'strings_to_formulas':False,'strings_to_urls':False,'strings_to_numbers':False}) as book:
        header=book.add_format({'bold':True,'font_color':'#FFFFFF','bg_color':'#'+GREEN,'text_wrap':True,'valign':'vcenter'})
        text=book.add_format({'num_format':'@'})
        percent=book.add_format({'num_format':'0.00"%"'})
        hyperlink=book.add_format({'font_color':'blue','underline':True})
        colors={key:book.add_format({'bg_color':color}) for key,color in [('fail','#FCE4D6'),('warning','#FFF2CC'),('other','#E2EFDA')]}
        overview=book.add_worksheet('Overview');overview.hide_gridlines(2)
        overview.merge_range('A1:G1',title,book.add_format({'font_size':21,'bold':True,'font_color':'#'+GREEN}));overview.set_row(0,38)
        overview.merge_range('A2:G2','Start here. Open All Students or an institution tab. Cards are in the Result Cards folder.',book.add_format({'font_size':12,'font_color':'#555555'}))
        queue_summary=queue_summary or summary
        overview.write_row(3,0,['Records in master',summary.get('total',0),'Saved',summary.get('verified',0),'Not yet saved',queue_summary.get('total',0)-queue_summary.get('verified',0)])
        for col,label,target in [(0,'All Students','All Students'),(1,'Open full student list','All Students'),(2,'Subject Marks','Subject Marks'),(3,'Open subject breakdown','Subject Marks')]:
            overview.write_url(4,col,f"internal:'{target}'!A1",hyperlink,label)
        overview.write_row(5,0,['Imported rolls',queue_summary.get('total',0)])
        overview.merge_range('A7:G7','Student details are retained even when the board provides no numeric total.')
        overview.write_row(8,0,['Institution / District','Students','Numeric total available','Student tab','Separate workbook'])
        groups=defaultdict(list)
        for record in records:
            if record['state']=='verified':groups[record.get('institution','')].append(record)
        names=unique_names(groups,fallback='Institution not provided',limit=31,reserved=('Overview','All Students','Subject Marks','Missing Results'))
        paths={entry['institution_district']:entry for entry in institutions}
        next_row=9
        for institution,members in sorted(groups.items()):
            entry=paths.get(institution);tab=names[institution]
            overview.write_row(next_row,0,[safe_text(institution or 'Institution not provided'),len(members),sum(r.get('obtained') is not None for r in members)])
            overview.write_url(next_row,3,f"internal:'{tab}'!A1",hyperlink,'Open students')
            if entry:overview.write_url(next_row,4,'external:'+str(Path(entry['folder'])/entry['workbook']),hyperlink,'Open report')
            next_row+=1
        next_row+=1;overview.write_row(next_row,0,['Board outcome','Students'])
        for status,count in sorted(Counter(r.get('result_status') or 'Outcome unavailable' for r in records if r['state']=='verified').items()):
            next_row+=1;overview.write_row(next_row,0,[safe_text(status),count])
        overview.freeze_panes(9,0)
        for col,width in enumerate([35,24,29,24,26,12,12]):overview.set_column(col,col,width)
        overview_highlight=book.add_format({'bg_color':'#E2EFDA','bold':True,'font_color':'#'+GREEN})
        overview.set_row(3,None,overview_highlight);overview.set_row(8,None,overview_highlight)

        text_headings={LABELS[k] for k in text_columns}
        def table_sheet(name,headers,rows,table_name):
            page=book.add_worksheet(name);page.freeze_panes(1,3);page.set_row(0,34)
            for col,heading in enumerate(headers):page.set_column(col,col,WIDTHS.get(heading,22))
            page.write_row(0,0,headers,header)
            count=0
            for count,values in enumerate(rows,1):
                for col,value in enumerate(values):
                    heading=headers[col]
                    fmt=text if heading in text_headings else percent if heading=='Percentage' else None
                    if heading=='Result Status' and value:fmt=colors['fail' if value=='FAIL' else 'warning' if value in ('ABSENT','MARKS NOT IMPROVED') else 'other']
                    page.write(count,col,safe_text(value),fmt)
            if count:
                page.add_table(0,0,count,len(headers)-1,{'name':table_name,'style':'Table Style Medium 4','columns':[{'header':h,'header_format':header} for h in headers]})
            else:page.autofilter(0,0,0,len(headers)-1)
            page.set_landscape();page.set_paper(8);page.fit_to_pages(1,0);page.repeat_rows(0)
            return page
        headings=[LABELS[column] for column in columns]
        def students(rows):return ([display(row,column) for column in columns] for row in rows)
        table_sheet('All Students',headings,students(records),'AllStudents')
        for number,(institution,members) in enumerate(sorted(groups.items()),1):
            table_sheet(names[institution],headings,students(members),f'Institution{number}')
        subjects=([record['roll'],record.get('institution'),*[subject.get(k) for k in SUBJECT_FIELDS]] for record in records for subject in record.get('subjects',[]))
        table_sheet('Subject Marks',['Roll No','Institution / District',*SUBJECT_LABELS],subjects,'SubjectMarks')
        missing=([r['roll'],display(r,'state'),r.get('error')] for r in records if r['state']!='verified')
        table_sheet('Missing Results',['Roll No','Collection Status','Lookup Issue'],missing,'MissingResults')
