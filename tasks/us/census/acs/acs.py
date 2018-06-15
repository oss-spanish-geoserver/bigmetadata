from time import time
import re
from collections import OrderedDict
from luigi import Parameter, WrapperTask, Task

from lib.timespan import get_timespan
from lib.logger import get_logger
from tasks.base_tasks import TableTask, MetaWrapper, LoadPostgresFromURL
from tasks.util import grouper
from tasks.us.census.tiger import SumLevel, ShorelineClip, TigerBlocksInterpolation
from tasks.us.census.tiger import (SUMLEVELS, GeoidColumns, GEOID_SUMLEVEL_COLUMN, GEOID_SHORELINECLIPPED_COLUMN)
from tasks.base_tasks import PostgresTarget
from tasks.meta import (current_session, GEOM_REF)
from .columns import QuantileColumns, Columns

LOGGER = get_logger(__name__)

STATE = 'state'
COUNTY = 'county'
CENSUS_TRACT = 'census_tract'
BLOCK_GROUP = 'block_group'
BLOCK = 'block'
PUMA = 'puma'
ZCTA5 = 'zcta5'
CONGRESSIONAL_DISTRICT = 'congressional_district'
SCHOOL_DISTRICT_ELEMENTARY = 'school_district_elementary'
SCHOOL_DISTRICT_SECONDARY = 'school_district_secondary'
SCHOOL_DISTRICT_UNIFIED = 'school_district_unified'
CBSA = 'cbsa'
PLACE = 'place'

SAMPLE_1YR = '1yr'
SAMPLE_5YR = '5yr'

GEOGRAPHIES = [STATE, COUNTY, CENSUS_TRACT, BLOCK_GROUP, BLOCK, PUMA, ZCTA5, CONGRESSIONAL_DISTRICT,
               SCHOOL_DISTRICT_ELEMENTARY, SCHOOL_DISTRICT_SECONDARY, SCHOOL_DISTRICT_UNIFIED,
               CBSA, PLACE]
YEARS = ['2015', '2014', '2010']
SAMPLES = [SAMPLE_5YR, SAMPLE_1YR]


class DownloadACS(LoadPostgresFromURL):

    # http://censusreporter.tumblr.com/post/73727555158/easier-access-to-acs-data
    url_template = 'https://s3.amazonaws.com/census-backup/acs/{year}/' \
            'acs{year}_{sample}/acs{year}_{sample}_backup.sql.gz'

    year = Parameter()
    sample = Parameter()

    @property
    def schema(self):
        return 'acs{year}_{sample}'.format(year=self.year, sample=self.sample)

    def run(self):
        cursor = current_session()
        cursor.execute('DROP SCHEMA IF EXISTS {schema} CASCADE'.format(schema=self.schema))
        self.load_from_url(self.url_template.format(year=self.year, sample=self.sample))


class AddBlockDataToACSTables(Task):
    year = Parameter()
    sample = Parameter()

    def requires(self):
        return {
            'interpolation': TigerBlocksInterpolation(year=2015),
            'acs_data': DownloadACS(year=self.year, sample=self.sample),
            'acs_column': Columns(year=self.year, sample=self.sample, geography='block_group')
        }
    def run(self):
        session = current_session()
        inputschema = 'acs{year}_{sample}'.format(year=self.year, sample=self.sample)
        table_ids = {}
        for _, coltarget in self.input()['acs_column'].items():
            colid = coltarget._id.split('.')[-1]
            tableid = colid.split('.')[-1][0:-3]
            table_ids[tableid.lower()] = {"schema": inputschema, "table": tableid.lower()}
        for table_data in table_ids.values():
            cols_clause_query = '''
                SELECT string_agg('(' || column_name || ' * percentage) ' || column_name, ', ')
                FROM information_schema.columns
                WHERE table_schema = '{schema}'
                AND table_name = '{table}' and column_name <> 'geoid'
            '''.format(schema=table_data['schema'], table=table_data['table'])
            cols_clause = session.execute(cols_clause_query).fetchone()[0]
            if not cols_clause:
                LOGGER.error('Error geting column names for the table {}'.format(table_data['table']))
                LOGGER.error('SQL {}'.format(cols_clause_query))
                continue
            add_blocks_query = '''
                SELECT bi.blockid, {cols}
                FROM "{schema}"."{table}" acs
                INNER JOIN {blockint} bi ON bi.blockgroupid = acs.geoid
                WHERE char_length(geoid) = 12
            '''.format(cols=cols_clause, schema=table_data['schema'], table=table_data['table'],
                       blockint=self.input()['interpolation'].table)
            resp = session.execute(add_blocks_query).fetchall()
            LOGGER.info('SQL: {}'.format(add_blocks_query))
            LOGGER.info('Results: {}'.format(len(resp)))
        LOGGER.info('fin')

class Quantiles(TableTask):
    '''
    Calculate the quantiles for each ACS column
    '''

    year = Parameter()
    sample = Parameter()
    geography = Parameter()

    def requires(self):
        return {
            'columns': QuantileColumns(year=self.year, sample=self.sample, geography=self.geography),
            'table': Extract(year=self.year,
                             sample=self.sample,
                             geography=self.geography),
            'tiger': GeoidColumns(),
            'sumlevel': SumLevel(geography=self.geography, year='2015'),
            'shorelineclip': ShorelineClip(geography=self.geography, year='2015')
        }

    def version(self):
        return 14

    def targets(self):
        return {
            self.input()['shorelineclip'].obs_table: GEOM_REF,
            self.input()['sumlevel'].obs_table: GEOM_REF,
        }

    def columns(self):
        input_ = self.input()
        columns = OrderedDict({
            'geoidsl': input_['tiger'][self.geography + GEOID_SUMLEVEL_COLUMN],
            'geoidsc': input_['tiger'][self.geography + GEOID_SHORELINECLIPPED_COLUMN]
        })
        columns.update(input_['columns'])
        return columns

    def table_timespan(self):
        sample = int(self.sample[0])
        return get_timespan('{start} - {end}'.format(start=int(self.year) - sample + 1,
                                                     end=int(self.year)))

    def populate(self):
        connection = current_session()
        input_ = self.input()
        quant_col_names = list(input_['columns'].keys())
        old_col_names = [name.split("_quantile")[0]
                         for name in quant_col_names]

        insert = True
        for cols in grouper(list(zip(quant_col_names, old_col_names)), 20):
            selects = [" percent_rank() OVER (ORDER BY {old_col} ASC) as {old_col} ".format(old_col=c[1])
                       for c in cols if c is not None]

            insert_statment = ", ".join([c[0] for c in cols if c is not None])
            old_cols_statment = ", ".join([c[1] for c in cols if c is not None])
            select_statment = ", ".join(selects)
            before = time()
            if insert:
                stmt = '''
                    INSERT INTO {table}
                    (geoidsl, geoidsc, {insert_statment})
                    SELECT geoidsl, geoidsc, {select_statment}
                    FROM {source_table}
                '''.format(
                    table=self.output().table,
                    insert_statment=insert_statment,
                    select_statment=select_statment,
                    source_table=input_['table'].table
                )
                insert = False
            else:
                stmt = '''
                    WITH data as (
                        SELECT geoidsl, geoidsc, {select_statment}
                        FROM {source_table}
                    )
                    UPDATE {table} SET ({insert_statment}) = ROW({old_cols_statment})
                    FROM data
                    WHERE data.geoidsc = {table}.geoidsc
                     AND data.geoidsl = {table}.geoidsl
                '''.format(
                    table=self.output().table,
                    insert_statment=insert_statment,
                    select_statment=select_statment,
                    old_cols_statment=old_cols_statment,
                    source_table=input_['table'].table
                )
            connection.execute(stmt)
            after = time()
            LOGGER.info('quantile calculation time taken : %s', int(after - before))


class Extract(TableTask):
    '''
    Generate an extract of important ACS columns for CartoDB
    '''

    year = Parameter()
    sample = Parameter()
    geography = Parameter()

    def version(self):
        return 14

    def requires(self):
        return {
            'acs': Columns(year=self.year, sample=self.sample, geography=self.geography),
            'tiger': GeoidColumns(),
            'data': DownloadACS(year=self.year, sample=self.sample),
            'sumlevel': SumLevel(geography=self.geography, year='2015'),
            'shorelineclip': ShorelineClip(geography=self.geography, year='2015')
        }

    def table_timespan(self):
        sample = int(self.sample[0])
        return get_timespan('{start} - {end}'.format(start=int(self.year) - sample + 1,
                                                     end=int(self.year)))

    def targets(self):
        return {
            self.input()['shorelineclip'].obs_table: GEOM_REF,
            self.input()['sumlevel'].obs_table: GEOM_REF,
        }

    def columns(self):
        input_ = self.input()
        cols = OrderedDict([
            ('geoidsl', input_['tiger'][self.geography + GEOID_SUMLEVEL_COLUMN]),
            ('geoidsc', input_['tiger'][self.geography + GEOID_SHORELINECLIPPED_COLUMN]),
        ])
        for colkey, col in input_['acs'].items():
            cols[colkey] = col
        return cols

    def populate(self):
        '''
        load relevant columns from underlying census tables
        '''
        session = current_session()
        sumlevel = SUMLEVELS[self.geography]['summary_level']
        colids = []
        colnames = []
        tableids = set()
        inputschema = 'acs{year}_{sample}'.format(year=self.year, sample=self.sample)
        for colname, coltarget in self.columns().items():
            colid = coltarget.get(session).id
            tableid = colid.split('.')[-1][0:-3]
            if 'geoid' in colid:
                colids.append('SUBSTR(geoid, 8)')
            else:
                colid = coltarget._id.split('.')[-1]
                resp = session.execute('SELECT COUNT(*) FROM information_schema.columns '
                                       "WHERE table_schema = '{inputschema}'  "
                                       "  AND table_name ILIKE '{inputtable}' "
                                       "  AND column_name ILIKE '{colid}' ".format(
                                           inputschema=inputschema,
                                           inputtable=tableid,
                                           colid=colid))
                if int(resp.fetchone()[0]) == 0:
                    continue
                colids.append(colid)
                tableids.add(tableid)
            colnames.append(colname)

        tableclause = '{inputschema}.{inputtable} '.format(
            inputschema=inputschema, inputtable=tableids.pop())
        for tableid in tableids:
            resp = session.execute('SELECT COUNT(*) FROM information_schema.tables '
                                   "WHERE table_schema = '{inputschema}'  "
                                   "  AND table_name ILIKE '{inputtable}' ".format(
                                       inputschema=inputschema,
                                       inputtable=tableid))
            if int(resp.fetchone()[0]) > 0:
                tableclause += ' JOIN {inputschema}.{inputtable} ' \
                               ' USING (geoid) '.format(inputschema=inputschema,
                                                        inputtable=tableid)
        table_id = self.output().table
        session.execute('INSERT INTO {output} ({colnames}) '
                        '  SELECT {colids} '
                        '  FROM {tableclause} '
                        '  WHERE geoid LIKE :sumlevelprefix '
                        ''.format(
                            output=table_id,
                            colnames=', '.join(colnames),
                            colids=', '.join(colids),
                            tableclause=tableclause
                        ), {
                            'sumlevelprefix': sumlevel + '00US%'
                        })


class ACSMetaWrapper(MetaWrapper):

    geography = Parameter()
    year = Parameter()
    sample = Parameter()

    params = {
        'geography': GEOGRAPHIES,
        'year': YEARS,
        'sample': SAMPLES
    }

    def tables(self):
        # no ZCTA for 2010
        if self.year == '2010' and self.geography == ZCTA5:
            pass
        # 1yr sample doesn't have block group or census_tract
        elif self.sample == SAMPLE_1YR and self.geography in (CENSUS_TRACT, BLOCK_GROUP, BLOCK, ZCTA5):
            pass
        else:
            yield Quantiles(geography=self.geography, year=self.year, sample=self.sample)


class ExtractAll(WrapperTask):

    year = Parameter()
    sample = Parameter()

    def requires(self):
        geographies = set(GEOGRAPHIES)
        if self.sample == SAMPLE_1YR:
            geographies.remove(ZCTA5)
            geographies.remove(BLOCK_GROUP)
            geographies.remove(BLOCK)
            geographies.remove(CENSUS_TRACT)
        elif self.year == '2010':
            geographies.remove(ZCTA5)
        for geo in geographies:
            if geo == BLOCK:
                yield QuantilesBlock(geography=geo, year=self.year, sample=self.sample)
            else:
                yield Quantiles(geography=geo, year=self.year, sample=self.sample)


class ACSAll(WrapperTask):

    def requires(self):
        for year in YEARS:
            for sample in SAMPLES:
                yield ExtractAll(year=year, sample=sample)