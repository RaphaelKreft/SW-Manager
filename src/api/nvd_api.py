"""
nvd_api.py: This file contains functions to query the REST API of NVD for CVE
"""

from datetime import datetime, timedelta
from ratelimiter import RateLimiter
import functools
import requests
import logging

#logging.warning("URL3LIB - Warnings are disabled!")
#requests.packages.urllib3.disable_warnings()


class NvdApi:
    _SEARCH_URL_SPECIFIC = 'https://services.nvd.nist.gov/rest/json/cve/1.0'
    _SEARCH_URL_MULTI = 'https://services.nvd.nist.gov/rest/json/cves/1.0'
    DETAIL_VIEW_PREFIX = 'https://nvd.nist.gov/vuln/detail/'

    def __init__(self, api_key=None):
        self.api_key = api_key
        self.period = 60    # 60 second period
        self.max_calls = 100 if api_key else 10
        self.rate_limiter = RateLimiter(max_calls=self.max_calls, period=self.period)

    def search_by_id(self, cve_id: str):
        """
        This method is not used currently but can later be modified to get information about specific cves.
        It takes a specific Vulnerability ID(CVE_Number) and returns a CVE Instance if a result is found.
        """
        data = self._request_specific_by_id(cve_id)
        if ('message' in data.keys() and data['message'].contains('Unable to find vuln')) or data['totalResults'] == 0:
            raise APIError(f"No data for cve with id {cve_id}. (resultcount = 0)")
        else:
            return CveResultList(data)

    def search_by_name_and_date(self, keyword: str, start_date: datetime = None):
        """
        This Method takes a keyword and a start_date which are the parameters for the search we initiate on NVD.
        This method should perform a query in a date range from given start_date up to current time. Therefor the
        range is split in parts of max 120 days to be able to work with the NVD API. If a query fails, an APIError is
        raised and this search is aborted.
        """
        now = datetime.now()
        max_delta = timedelta(days=120)
        ranges = []
        # prepare date-ranges of max 120 days to be able to query NVD API
        my_delta = now - start_date
        curr_date = start_date
        while my_delta > max_delta:
            ranges.append((curr_date, curr_date + max_delta))
            curr_date += max_delta
            my_delta = now - curr_date
        ranges.append((curr_date, now))
        # perform queries
        logging.debug(f"{keyword.upper()} -- After processing, ranges has len {len(ranges)}")
        results = []
        for start, end in ranges:
            results.append(CveResultList(self._name_date_query(keyword, start, end)))
        logging.debug(f"{keyword.upper()} -- After performing queries, results has len {len(results)}")
        res_list = functools.reduce(CveResultList.add, results, CveResultList([]))
        return res_list

    def _perform_request(self, url: str, parameters: dict, verify=False):
        """
        Performs a get request towards an API while using a rate limiter -
        """
        with self.rate_limiter:
            response = requests.get(url=url, params=parameters, verify=verify)
            if response.ok:
                return response.json()
            else:
                raise APIError(f"API call for {url} not 'ok' \n\n")

    def _request_specific_by_id(self, cve_id: str):
        """
        Request data for a specific CVE-ID, returns the answer json
        """
        search_url = f'{self._SEARCH_URL_SPECIFIC}/{cve_id}'
        return self._perform_request(search_url, parameters={}, verify=False)

    def _name_date_query(self, keyword: str, start_date: datetime = None, end_date: datetime = None):
        """
        build a request for the nvd api, by using date and keyword parameters. Then sends request and returns result
        as json
        """
        # if start_date not given, just search for keyword, else include start_date as search paramete
        pars = {'keyword': keyword, 'pubStartDate': start_date.strftime("%Y-%m-%dT%H:%M:%S:000 UTC-05:00"),
                'pubEndDate': end_date.strftime("%Y-%m-%dT%H:%M:%S:000 UTC-05:00")}
        response_json = self._perform_request(url=NvdApi._SEARCH_URL_MULTI, parameters=pars, verify=False)
        logging.debug(f"Request for {keyword}, start: {start_date.strftime('%Y-%m-%dT%H:%M:%S')} , end: "
                      f"{end_date.strftime('%Y-%m-%dT%H:%M:%S')} is okay!")
        return response_json


class Cve:
    """
    This class is an abstraction of a CVE-Entry. In this Version just the necessary Data is included.
    """

    def __init__(self, cve_id, published_date, last_modified_date, severity):
        self.cve_id = cve_id
        self.published_date = published_date
        self.last_modified_date = last_modified_date
        self.severity = severity

    def __str__(self):
        return f"CVE: {self.cve_id}\n- published_date: {self.published_date}\n" \
               f"- last_modified: {self.last_modified_date}\n- severity: {self.severity}"

    def get_url(self, check_is_up=False):
        """
        This Method assembles an URL for this specific CVE. The URL is checked whether
        it is reachable or not. If it is reachable, the url is returned, if not just the cve_ID
        is returned.
        """
        url = f"{NvdApi.DETAIL_VIEW_PREFIX}{self.cve_id}"
        if check_is_up:
            is_up = requests.head(url).status_code == 200
            if is_up:
                return url
            else:
                return None
        else:
            return url

    @staticmethod
    def result_to_cve(result_json):
        """
        Method to make an CVe instance from a json received by the api
        """
        mod_date = datetime.strptime(result_json['lastModifiedDate'], "%Y-%m-%dT%H:%MZ")
        pub_date = datetime.strptime(result_json['publishedDate'], "%Y-%m-%dT%H:%MZ")
        cve_id = result_json['cve']['CVE_data_meta']['ID']
        if result_json['impact'] == {}:
            severity = "UNKNOWN"
        else:
            severity = result_json['impact']['baseMetricV2']['severity']
        return Cve(cve_id, pub_date, mod_date, severity)


class CveResultList:
    """
    Instances of this class are used to represent/store lists of results received by the api.
    """

    def __init__(self, json_result):
        if type(json_result) is list:
            self.results = json_result
        else:
            self.results = []
            self.num_results = None
            if json_result is not None:
                self._parse_json(json_result)

    def get_latest(self):
        """
        This method returns the most current CVE from the list of results
        """
        if self.results is not None:
            return max(self.results, key=lambda x: x.published_date)

    def get_cve_id_list(self, make_urls=False):
        """
        Returns a List of tuples (CVE-ID, URL). If make_urls is set False,
        then the method get_url will not be executed but None as placeholder
        will be inserted.
        """
        if make_urls:
            return [(cve.cve_id, cve.get_url()) for cve in self.results]
        else:
            return [(cve.cve_id, None) for cve in self.results]

    def get_max_severity(self):
        """
        Finds the Cve with the highest severity and returns this severity.
        Returns None when result-list of cve's is empty.
        """
        if not self.results:
            return None
        return max(self.results, key=self._severity_ranking).severity

    def _parse_json(self, json_result):
        """
        This method takes the complete result_json from the api and parse it.
        """
        self.num_results = json_result["totalResults"]
        for cve_json in json_result["result"]["CVE_Items"]:
            self.results.append(Cve.result_to_cve(cve_json))

    @staticmethod
    def add(first, other):
        """
        Adding up Instances of this class means bulding a union of the result lists.
        This union will be saved in self object
        """
        merged_results = first.results
        for o in other.results:
            if o.cve_id not in first.get_cve_id_list():
                merged_results.append(o)
        return CveResultList(merged_results)

    @staticmethod
    def _severity_ranking(cve):
        """
        Takes a cve object and returns a number according to the severity of the CVE.
        This function is meant to be used as helper to be able to rank the cve's by their severity.
        """
        severities = {"UNKNOWN": -1, "LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
        return severities[cve.severity]


class APIError(Exception):
    """
    Exception-class to represent an Error within the operation of an NvdApi instance.
    This class gets a message and logs it
    """

    def __init__(self, message):
        super().__init__(f"APIError: {message}")
        logging.error(f"APIError: {message}")