"""Real Copart US auction yards.

Coordinates are best-effort approximations from the city in the address column — accurate
to within a few miles for major metros, less precise for small towns where the state's
general region is used. Good enough for a demo-grade national map.

Format: (code, location_name, address, city, state, zip, lat, lng)
"""

COPART_YARDS: list[tuple[str, str, str, str, str, str, float, float]] = [
    # Alabama
    ("AL-TAN", "Tanner",          "20760 Sandy Road",                 "Tanner",            "AL", "35671", 34.7186, -86.9261),
    ("AL-BIR", "Birmingham",      "3101 Davey Allison Blvd",          "Hueytown",          "AL", "35023", 33.4509, -86.9961),
    ("AL-MGM", "Montgomery",      "6044 Troy Highway",                "Montgomery",        "AL", "36116", 32.3007, -86.2375),
    ("AL-MOB", "Mobile",          "4763 Lott Road",                   "Eight Mile",        "AL", "36613", 30.8294, -88.1311),
    # Alaska
    ("AK-ANC", "Anchorage",       "401 W Chipperfield Dr",            "Anchorage",         "AK", "99501", 61.2181, -149.9003),
    # Arizona
    ("AZ-PHX", "Phoenix",         "615 So. 51st Avenue",              "Phoenix",           "AZ", "85043", 33.4248, -112.1814),
    ("AZ-TUC", "Tucson",          "5600 S. Arcadia Ave",              "Tucson",            "AZ", "85706", 32.1326, -110.9133),
    # Arkansas
    ("AR-LIT", "Little Rock",     "703 Main Street",                  "Conway",            "AR", "72032", 35.0887, -92.4421),
    ("AR-FAY", "Fayetteville",    "15976 Bill Campbell Road",         "Prairie Grove",     "AR", "72753", 35.9759, -94.3175),
    # California
    ("CA-VAL", "Vallejo",            "282 Fifth Street",                 "Vallejo",         "CA", "94590", 38.1041, -122.2566),
    ("CA-SAC", "Sacramento",         "8600 Morrison Creek Dr",           "Sacramento",      "CA", "95828", 38.4940, -121.4006),
    ("CA-HAY", "Hayward",            "1964 Sabre Street",                "Hayward",         "CA", "94545", 37.6494, -122.1208),
    ("CA-FRE", "Fresno",             "1255 East Central",                "Fresno",          "CA", "93725", 36.7041, -119.7468),
    ("CA-BAK", "Bakersfield",        "2216 Coy Avenue",                  "Bakersfield",     "CA", "93307", 35.3434, -119.0008),
    ("CA-SJC", "San Jose",           "13895 Llagas Ave",                 "San Martin",      "CA", "95046", 37.0915, -121.6164),
    ("CA-SBE", "San Bernardino",     "1203 S. Rancho Ave",               "Colton",          "CA", "92324", 34.0739, -117.3137),
    ("CA-LAX", "Los Angeles",        "8423 South Alameda",               "Los Angeles",     "CA", "90001", 33.9645, -118.2393),
    ("CA-SAS", "South Sacramento",   "8687 Weyand Ave",                  "Sacramento",      "CA", "95828", 38.4868, -121.3938),
    ("CA-VAN", "Van Nuys",           "7519 Woodman Ave",                 "Van Nuys",        "CA", "91405", 34.2042, -118.4476),
    ("CA-SAN", "San Diego",          "7847 Airway Rd",                   "San Diego",       "CA", "92154", 32.5560, -117.0383),
    ("CA-MAR", "Martinez",           "2701 Waterfront Road",             "Martinez",        "CA", "94553", 38.0193, -122.1241),
    ("CA-RAN", "Rancho Cucamonga",   "12167 Arrow Route",                "Rancho Cucamonga","CA", "91739", 34.1233, -117.5150),
    ("CA-SUN", "Sun Valley",         "11409 Penrose Street",             "Sun Valley",      "CA", "91352", 34.2189, -118.3729),
    # Colorado
    ("CO-DEN", "Denver",          "1281 County Road 27",              "Brighton",          "CO", "80603", 39.9854, -104.8268),
    ("CO-CSP", "Colorado Springs","3701 N. Nevada Ave",               "Colorado Springs",  "CO", "80907", 38.8714, -104.8262),
    ("CO-DES", "Denver South",    "6464 Downing Street",              "Denver",            "CO", "80229", 39.8429, -104.9763),
    # Connecticut
    ("CT-HFD", "Hartford",        "138 Christian Lane",               "New Britain",       "CT", "06051", 41.6604, -72.7891),
    # Delaware
    ("DE-SEA", "Seaford",         "26029 Bethel Concord Road",        "Seaford",           "DE", "19973", 38.6412, -75.6113),
    # Florida
    ("FL-MIN", "Miami North",       "12850 NW 27th Ave.",              "Miami",            "FL", "33054", 25.8856, -80.2456),
    ("FL-TPS", "Tampa South",       "12020 US Highway 301 South",      "Riverview",        "FL", "33578", 27.8527, -82.3287),
    ("FL-JXW", "Jacksonville West", "450 Hammond Blvd",                "Jacksonville",     "FL", "32220", 30.3219, -81.8506),
    ("FL-ORL", "Orlando",           "307 East Landstreet Road",        "Orlando",          "FL", "32824", 28.3974, -81.3989),
    ("FL-WPB", "West Palm Beach",   "7876 Belvedere Road",             "West Palm Beach",  "FL", "33411", 26.7114, -80.1965),
    ("FL-FPI", "Fort Pierce",       "2601 Center Road",                "Fort Pierce",      "FL", "34946", 27.4486, -80.3358),
    ("FL-MIC", "Miami Central",     "11858 NW 36th Ave",               "Miami",            "FL", "33167", 25.9105, -80.2237),
    ("FL-OCA", "Ocala",             "7100 NW 44 Ave",                  "Ocala",            "FL", "34482", 29.2148, -82.1837),
    ("FL-TAL", "Tallahassee",       "1825 Commerce Blvd",              "Midway",           "FL", "32343", 30.4951, -84.4485),
    ("FL-JXE", "Jacksonville East", "5007 New Kings Rd",               "Jacksonville",     "FL", "32209", 30.3640, -81.6964),
    ("FL-PGD", "Punta Gorda",       "5017 Duncan Road",                "Punta Gorda",      "FL", "33982", 26.9447, -81.7898),
    ("FL-MIS", "Miami South",       "24301 SW 137th Ave",              "Homestead",        "FL", "33032", 25.4937, -80.4445),
    ("FL-ORN", "Orlando North",     "3351 W Orange Blossom Trail",     "Apopka",           "FL", "32712", 28.6913, -81.5128),
    # Georgia
    ("GA-ATW", "Atlanta West",       "2568 Old Alabama Road",          "Austell",          "GA", "30168", 33.8128, -84.6361),
    ("GA-SAV", "Savannah",           "5510 Silk Hope Road",            "Savannah",         "GA", "31405", 32.0292, -81.1517),
    ("GA-TIF", "Tifton",             "399 Oakridge Church Rd",         "Tifton",           "GA", "31794", 31.4500, -83.5085),
    ("GA-ATE", "Atlanta East",       "6089 Highway 20",                "Loganville",       "GA", "30052", 33.8389, -83.9007),
    ("GA-ATS", "Atlanta South",      "761 Clark Drive",                "Ellenwood",        "GA", "30294", 33.6271, -84.2733),
    ("GA-CTV", "Cartersville",       "1880 Hwy 113",                   "Cartersville",     "GA", "30120", 34.1652, -84.7999),
    ("GA-ATN", "Atlanta North",      "1602 Athens Highway",            "Gainesville",      "GA", "30507", 34.2960, -83.8087),
    # Hawaii
    ("HI-HON", "Honolulu",          "91-542 Awakumoku St",             "Kapolei",          "HI", "96707", 21.3357, -158.0578),
    # Idaho
    ("ID-BOI", "Boise",             "3716 North Middleton Road",       "Nampa",            "ID", "83651", 43.5816, -116.5777),
    # Illinois
    ("IL-CHN", "Chicago North",     "1475 Bluff City Blvd",            "Elgin",            "IL", "60120", 42.0354, -88.2820),
    ("IL-PEO", "Peoria",            "14417 VFW Road",                  "Pekin",            "IL", "61554", 40.5675, -89.6406),
    ("IL-CHS", "Chicago South",     "89 E. Sauk Trail",                "Chicago Heights",  "IL", "60411", 41.5061, -87.6356),
    ("IL-WHE", "Wheeling",          "110 East Palatine Road",          "Wheeling",         "IL", "60090", 42.1392, -87.9290),
    # Indiana
    ("IN-IND", "Indianapolis",      "4040 Office Plaza Blvd",          "Indianapolis",     "IN", "46254", 39.8403, -86.2520),
    ("IN-FTW", "Fort Wayne",        "696 East State Road 26",          "Hartford City",    "IN", "47348", 40.4506, -85.3691),
    ("IN-HMM", "Hammond",           "1849 Summer St",                  "Hammond",          "IN", "46320", 41.5834, -87.5000),
    # Iowa
    ("IA-DSM", "Des Moines",        "3300 Vandalia Road",              "Des Moines",       "IA", "50317", 41.6005, -93.5394),
    ("IA-DAV", "Davenport",         "3601 S 1st Street",               "Eldridge",         "IA", "52748", 41.6603, -90.5818),
    # Kansas
    ("KS-KAN", "Kansas City",       "6211 Kansas Ave.",                "Kansas City",      "KS", "66111", 39.0997, -94.7368),
    ("KS-WIC", "Wichita",           "4510 S Madison",                  "Wichita",          "KS", "67216", 37.6420, -97.2861),
    # Kentucky
    ("KY-LXW", "Lexington West",    "1051 Industry Road",              "Lawrenceburg",     "KY", "40342", 38.0382, -84.8967),
    ("KY-LXE", "Lexington East",    "5801 Kasp Ct",                    "Lexington",        "KY", "40509", 37.9986, -84.4317),
    ("KY-WAL", "Walton",            "13273 Dixie Highway",             "Walton",           "KY", "41094", 38.8748, -84.6094),
    ("KY-LOU", "Louisville",        "3100 Pond Station Road",          "Louisville",       "KY", "40219", 38.1448, -85.7156),
    # Louisiana
    ("LA-BTR", "Baton Rouge",       "21595 Greenwell Springs Road",    "Greenwell Springs","LA", "70739", 30.5683, -90.9764),
    ("LA-NOL", "New Orleans",       "14600 Old Gentilly Road",         "New Orleans",      "LA", "70129", 30.0182, -89.8853),
    ("LA-SHV", "Shreveport",        "5235 Greenwood Rd",               "Shreveport",       "LA", "71109", 32.4660, -93.8311),
    # Maine
    ("ME-LYM", "Lyman",             "136 Kennebunk Pond Road",         "Lyman",            "ME", "04002", 43.5273, -70.6592),
    # Maryland
    ("MD-WDC", "Washington DC",     "11055 Billingsley Road",          "Waldorf",          "MD", "20602", 38.6307, -76.9095),
    ("MD-BAL", "Baltimore",         "2251 Old Westminster Pike",       "Finksburg",        "MD", "21048", 39.4823, -76.9341),
    # Massachusetts
    ("MA-BOS", "Boston South",      "82 Cape Road",                    "Mendon",           "MA", "01756", 42.0937, -71.5546),
    ("MA-BON", "Boston North",      "55R High St",                     "North Billerica",  "MA", "01862", 42.5817, -71.2912),
    ("MA-WAR", "West Warren",       "600 Old West Warren Rd",          "West Warren",      "MA", "01092", 42.2098, -72.2381),
    # Michigan
    ("MI-DET", "Detroit",           "21000 Hayden Drive",              "Woodhaven",        "MI", "48183", 42.1389, -83.2410),
    ("MI-LAN", "Lansing",           "3902 South Canal Rd",             "Lansing",          "MI", "48917", 42.6878, -84.6730),
    ("MI-KIN", "Kincheloe",         "5030 W Kincheloe Road",           "Kincheloe",        "MI", "49788", 46.2674, -84.4682),
    ("MI-FLT", "Flint",             "5000 N State Road",               "Davison",          "MI", "48423", 43.0392, -83.4799),
    ("MI-ION", "Ionia",             "8460 S State Road",               "Portland",         "MI", "48875", 42.8497, -84.9116),
    # Minnesota
    ("MN-MIN", "Minneapolis",       "3737 East River Road",            "Fridley",          "MN", "55421", 45.0608, -93.2647),
    ("MN-STC", "Saint Cloud",       "200 County Road 159",             "Avon",             "MN", "56310", 45.6063, -94.4496),
    ("MN-MNN", "Minneapolis North", "1526 Bunker Lake Blvd",           "Ham Lake",         "MN", "55304", 45.2589, -93.2497),
    # Mississippi
    ("MS-JAC", "Jackson",           "205 S. Rankin Industrial Drive",  "Florence",         "MS", "39073", 32.1462, -90.1331),
    # Missouri
    ("MO-STL", "Saint Louis",       "13033 Taussig Ave",               "Bridgeton",        "MO", "63044", 38.7475, -90.4282),
    ("MO-SPF", "Springfield",       "2889 E. US Highway 60",           "Rogersville",      "MO", "65742", 37.1242, -93.0382),
    ("MO-COL", "Columbia Missouri", "8485 Richland Rd",                "Columbia",         "MO", "65201", 38.8902, -92.2362),
    ("MO-SIK", "Sikeston",          "687 E Outer Rd",                  "Sikeston",         "MO", "63801", 36.8770, -89.5878),
    # Montana
    ("MT-HEL", "Helena",            "3333 Bozeman Avenue",             "Helena",           "MT", "59601", 46.5891, -112.0391),
    ("MT-BIL", "Billings",          "1090 Island Park Rd",             "Billings",         "MT", "59101", 45.7833, -108.5007),
    # Nebraska
    ("NE-LIN", "Lincoln",           "13603 238th St",                  "Greenwood",        "NE", "68366", 40.9647, -96.4408),
    # Nevada
    ("NV-LAS", "Las Vegas",         "4810 N. Lamb Blvd",               "Las Vegas",        "NV", "89115", 36.2270, -115.0834),
    ("NV-REN", "Reno",              "9915 N. Virginia Street",         "Reno",             "NV", "89506", 39.6272, -119.8780),
    # New Hampshire
    ("NH-CAN", "Candia",            "134 Raymond Road",                "Candia",           "NH", "03034", 43.0529, -71.2787),
    # New Jersey
    ("NJ-GLA", "Glassboro",         "200 Grove St.",                   "Glassboro",        "NJ", "08028", 39.7065, -75.1063),
    ("NJ-SOM", "Somerville",        "2124 West Camplain Road",         "Hillsborough",     "NJ", "08844", 40.5067, -74.6552),
    ("NJ-TRE", "Trenton",           "108 N. Main Street",              "Windsor",          "NJ", "08561", 40.2462, -74.5852),
    # New Mexico
    ("NM-ABQ", "Albuquerque",       "7705 Broadway Se",                "Albuquerque",      "NM", "87105", 35.0089, -106.6410),
    # New York
    ("NY-NEW", "Newburgh",          "25 Riverview Drive",              "Marlboro",         "NY", "12542", 41.6118, -73.9637),
    ("NY-SYR", "Syracuse",          "46 Zuk-Pierce Rd",                "Central Square",   "NY", "13036", 43.2876, -76.1450),
    ("NY-LON", "Long Island",       "1983 Montauk Highway",            "Brookhaven",       "NY", "11719", 40.7765, -72.9151),
    ("NY-ROC", "Rochester",         "4 West Ave",                      "Leroy",            "NY", "14482", 42.9806, -77.9856),
    ("NY-ALB", "Albany",            "1916 Central Ave",                "Albany",           "NY", "12205", 42.7158, -73.8444),
    # North Carolina
    ("NC-CHI", "China Grove",       "1081 Recovery Road",              "China Grove",      "NC", "28023", 35.5722, -80.5839),
    ("NC-RAL", "Raleigh",           "310 Copart Road",                 "Dunn",             "NC", "28334", 35.2949, -78.6097),
    ("NC-MEB", "Mebane",            "1870 US 70 Hwy",                  "Mebane",           "NC", "27302", 36.0959, -79.2670),
    # Ohio
    ("OH-COL", "Columbus",          "1680 Williams Road",              "Columbus",         "OH", "43207", 39.9112, -82.9568),
    ("OH-CLE", "Cleveland East",    "286 East Twinsburg Road",         "Northfield",       "OH", "44067", 41.3450, -81.5184),
    ("OH-CLW", "Cleveland West",    "34417 E. Royalton Road",          "Columbia Station", "OH", "44028", 41.3261, -81.9290),
    ("OH-DAY", "Dayton",            "4691 Springboro Pike",            "Moraine",          "OH", "45439", 39.6840, -84.2226),
    # Oklahoma
    ("OK-OKC", "Oklahoma City",     "2829 SE 15th St",                 "Oklahoma City",    "OK", "73129", 35.4500, -97.4717),
    ("OK-TUL", "Tulsa",             "2408 W 21st Street",              "Tulsa",            "OK", "74107", 36.1357, -96.0167),
    # Oregon
    ("OR-PDN", "Portland North",    "6900 N.E. Cornfoot Drive",        "Portland",         "OR", "97218", 45.5934, -122.5953),
    ("OR-EUG", "Eugene",            "29815 Enid Road East",            "Eugene",           "OR", "97402", 44.0521, -123.1750),
    ("OR-PDS", "Portland South",    "2885 National Way",               "Woodburn",         "OR", "97071", 45.1437, -122.8551),
    # Pennsylvania
    ("PA-PHI", "Philadelphia",      "2704 Geryville Pike",             "Pennsburg",        "PA", "18073", 40.3925, -75.5752),
    ("PA-PTN", "Pittsburgh North",  "2000 River Road",                 "Ellwood City",     "PA", "16117", 40.8623, -80.2870),
    ("PA-HBG", "Harrisburg",        "8 Park Drive",                    "Grantville",       "PA", "17028", 40.3848, -76.6786),
    ("PA-PTS", "Pittsburgh South",  "526 Thompson Run Rd",             "West Mifflin",     "PA", "15122", 40.3640, -79.9095),
    ("PA-YOR", "York Haven",        "795 Sipe Rd",                     "York Haven",       "PA", "17370", 40.1117, -76.7188),
    ("PA-CHB", "Chambersburg",      "2962 Lincoln Way West",           "Chambersburg",     "PA", "17201", 39.9380, -77.6611),
    ("PA-ALT", "Altoona",           "4007 Admiral Peary Hwy",          "Ebensburg",        "PA", "15931", 40.4845, -78.7400),
    ("PA-SCR", "Scranton",          "210 Mcalpine Street",             "Duryea",           "PA", "18642", 41.3415, -75.7568),
    ("PA-PTE", "Pittsburgh East",   "133 Asphalt Lane",                "Adamsburg",        "PA", "15611", 40.3145, -79.6531),
    ("PA-PHE", "Philadelphia East", "77 Bristol Road",                 "Chalfont",         "PA", "18914", 40.2898, -75.2102),
    # South Carolina
    ("SC-CSC", "Columbia SC",       "4324 Highway 321 South",          "Gaston",           "SC", "29053", 33.8157, -81.1170),
    ("SC-GRE", "Greer",             "2465 Highway 101 South",          "Greer",            "SC", "29651", 34.9173, -82.2401),
    # Tennessee
    ("TN-MEM", "Memphis",           "5545 Swinnea Rd",                 "Memphis",          "TN", "38118", 35.0431, -89.9512),
    ("TN-NAS", "Nashville",         "865 Stumpy Lane",                 "Lebanon",          "TN", "37090", 36.2074, -86.2933),
    ("TN-KNX", "Knoxville",         "6355 B Highway 411",              "Madisonville",     "TN", "37354", 35.5234, -84.3618),
    # Texas
    ("TX-HOU", "Houston",           "1655 Rankin Road",                "Houston",          "TX", "77073", 29.9810, -95.3859),
    ("TX-DAL", "Dallas",            "505 Idlewild Road",               "Grand Prairie",    "TX", "75051", 32.7211, -97.0089),
    ("TX-LUF", "Lufkin",            "3700 Old Union Road",             "Lufkin",           "TX", "75904", 31.3382, -94.7291),
    ("TX-LNG", "Longview",          "3046 Highway 322 South",          "Longview",         "TX", "75603", 32.4421, -94.6979),
    ("TX-ELP", "El Paso",           "501 Valley Chili Road",           "Anthony",          "TX", "79821", 31.9926, -106.6098),
    ("TX-AUS", "Austin",            "8725 IH - 35 N",                  "New Braunfels",    "TX", "78130", 29.7300, -98.0686),
    ("TX-MCA", "McAllen",           "301 Mile 1 East",                 "Mercedes",         "TX", "78570", 26.1487, -97.9145),
    ("TX-ABI", "Abilene",           "2630 Farm to Market Road 3034",   "Abilene",          "TX", "79601", 32.4487, -99.7331),
    ("TX-SAT", "San Antonio",       "11130 Applewhite Rd",             "San Antonio",      "TX", "78224", 29.2745, -98.5294),
    ("TX-AMA", "Amarillo",          "3999 S Loop 335 E",               "Amarillo",         "TX", "79118", 35.1410, -101.7950),
    ("TX-CRP", "Corpus Christi",    "3200 Agnes Street",               "Corpus Christi",   "TX", "78405", 27.7794, -97.4445),
    ("TX-FTW", "Fort Worth",        "950 Blue Mound Road West",        "Haslet",           "TX", "76052", 32.9710, -97.3473),
    ("TX-DAS", "Dallas South",      "1701 East Beltline Road",         "Wilmer",           "TX", "75172", 32.5798, -96.6868),
    ("TX-WAC", "Waco",              "7201 N General Bruce Dr",         "Temple",           "TX", "76501", 31.1207, -97.4131),
    # Utah
    ("UT-SLC", "Salt Lake City",    "170 W. Center Street",            "North Salt Lake",  "UT", "84054", 40.8475, -111.9239),
    # Virginia
    ("VA-DAN", "Danville",          "12360 US Hwy 29",                 "Chatham",          "VA", "24531", 36.8262, -79.3998),
    ("VA-HAM", "Hampton",           "16 Nettles Lane",                 "Hampton",          "VA", "23666", 37.0299, -76.3452),
    ("VA-RIC", "Richmond",          "5701 Whiteside Rd",               "Sandston",         "VA", "23150", 37.5246, -77.3197),
    # Washington
    ("WA-SEA", "North Seattle",     "16701 51st Ave NE",               "Arlington",        "WA", "98223", 48.1985, -122.1465),
    ("WA-GRA", "Graham",            "21421 Meridian E",                "Graham",           "WA", "98338", 47.0537, -122.2987),
    ("WA-PAS", "Pasco",             "3333 N. Railroad Avenue",         "Pasco",            "WA", "99301", 46.2396, -119.1006),
    ("WA-SPO", "Spokane",           "11019 West Mcfarlane Road",       "Airway Heights",   "WA", "99001", 47.6447, -117.5933),
    # West Virginia
    ("WV-CHA", "Charleston",        "2481 US Route 60",                "Hurricane",        "WV", "25526", 38.4334, -82.0099),
    # Wisconsin
    ("WI-MAD", "Madison",           "5448 Lien Road",                  "Madison",          "WI", "53718", 43.1253, -89.2942),
    ("WI-MIL", "Milwaukee",         "4825 S. Whitnall Ave",            "Cudahy",           "WI", "53110", 42.9522, -87.8650),
]


# State-level base hourly wages for Copart yards. Used by the seed to assign a defensible
# per-yard wage = state_base + deterministic per-yard offset. Higher in states with higher
# RPP / known higher hourly rates for warehouse-adjacent work.
STATE_BASE_WAGE: dict[str, float] = {
    "AL": 14.00, "AK": 19.50, "AZ": 16.00, "AR": 14.00,
    "CA": 19.50, "CO": 17.50, "CT": 18.00, "DE": 17.00, "DC": 18.00,
    "FL": 15.50, "GA": 15.50, "HI": 18.50, "ID": 15.00,
    "IL": 18.00, "IN": 15.00, "IA": 15.00, "KS": 14.50,
    "KY": 14.50, "LA": 14.50, "ME": 16.00, "MD": 17.50,
    "MA": 19.00, "MI": 16.00, "MN": 17.50, "MS": 13.50,
    "MO": 15.00, "MT": 14.50, "NE": 14.50, "NV": 17.00,
    "NH": 16.50, "NJ": 19.00, "NM": 14.00, "NY": 18.50,
    "NC": 15.00, "ND": 15.00, "OH": 15.50, "OK": 14.00,
    "OR": 17.50, "PA": 16.00, "RI": 17.00, "SC": 14.50,
    "SD": 14.50, "TN": 14.50, "TX": 16.50, "UT": 15.50,
    "VT": 16.50, "VA": 15.50, "WA": 18.50, "WV": 14.50,
    "WI": 15.50, "WY": 14.50,
}
