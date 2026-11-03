"""Static tables for `seed_scale_dataset`: the cities and the locality names.

Leading underscore: Django would otherwise list this module as a command.

Every row is public geography (a city name and its centre point). Nothing here
is personal data. Cities are listed largest first within each country; the seed
uses that order as the size rank for its long-tail weighting.
"""

# (name, state_code, state_name, lat, lng)
INDIA = [
    ('Hyderabad', 'TG', 'Telangana', 17.3850, 78.4867),
    ('Bengaluru', 'KA', 'Karnataka', 12.9716, 77.5946),
    ('Mumbai', 'MH', 'Maharashtra', 19.0760, 72.8777),
    ('Delhi', 'DL', 'Delhi', 28.6139, 77.2090),
    ('Chennai', 'TN', 'Tamil Nadu', 13.0827, 80.2707),
    ('Pune', 'MH', 'Maharashtra', 18.5204, 73.8567),
    ('Kolkata', 'WB', 'West Bengal', 22.5726, 88.3639),
    ('Ahmedabad', 'GJ', 'Gujarat', 23.0225, 72.5714),
    ('Jaipur', 'RJ', 'Rajasthan', 26.9124, 75.7873),
    ('Surat', 'GJ', 'Gujarat', 21.1702, 72.8311),
    ('Lucknow', 'UP', 'Uttar Pradesh', 26.8467, 80.9462),
    ('Kochi', 'KL', 'Kerala', 9.9312, 76.2673),
    ('Visakhapatnam', 'AP', 'Andhra Pradesh', 17.6868, 83.2185),
    ('Chandigarh', 'CH', 'Chandigarh', 30.7333, 76.7794),
    ('Indore', 'MP', 'Madhya Pradesh', 22.7196, 75.8577),
    ('Nagpur', 'MH', 'Maharashtra', 21.1458, 79.0882),
    ('Coimbatore', 'TN', 'Tamil Nadu', 11.0168, 76.9558),
    ('Vijayawada', 'AP', 'Andhra Pradesh', 16.5062, 80.6480),
    ('Bhopal', 'MP', 'Madhya Pradesh', 23.2599, 77.4126),
    ('Patna', 'BR', 'Bihar', 25.5941, 85.1376),
    ('Vadodara', 'GJ', 'Gujarat', 22.3072, 73.1812),
    ('Thiruvananthapuram', 'KL', 'Kerala', 8.5241, 76.9366),
    ('Mysuru', 'KA', 'Karnataka', 12.2958, 76.6394),
    ('Guwahati', 'AS', 'Assam', 26.1445, 91.7362),
    ('Bhubaneswar', 'OD', 'Odisha', 20.2961, 85.8245),
    ('Nashik', 'MH', 'Maharashtra', 19.9975, 73.7898),
    ('Ludhiana', 'PB', 'Punjab', 30.9010, 75.8573),
    ('Kanpur', 'UP', 'Uttar Pradesh', 26.4499, 80.3319),
    ('Agra', 'UP', 'Uttar Pradesh', 27.1767, 78.0081),
    ('Varanasi', 'UP', 'Uttar Pradesh', 25.3176, 82.9739),
    ('Rajkot', 'GJ', 'Gujarat', 22.3039, 70.8022),
    ('Madurai', 'TN', 'Tamil Nadu', 9.9252, 78.1198),
    ('Warangal', 'TG', 'Telangana', 17.9689, 79.5941),
    ('Mangaluru', 'KA', 'Karnataka', 12.9141, 74.8560),
    ('Jodhpur', 'RJ', 'Rajasthan', 26.2389, 73.0243),
    ('Ranchi', 'JH', 'Jharkhand', 23.3441, 85.3096),
    ('Raipur', 'CG', 'Chhattisgarh', 21.2514, 81.6296),
    ('Dehradun', 'UT', 'Uttarakhand', 30.3165, 78.0322),
    ('Amritsar', 'PB', 'Punjab', 31.6340, 74.8723),
    ('Jabalpur', 'MP', 'Madhya Pradesh', 23.1815, 79.9864),
    ('Gwalior', 'MP', 'Madhya Pradesh', 26.2183, 78.1828),
    ('Tiruchirappalli', 'TN', 'Tamil Nadu', 10.7905, 78.7047),
    ('Salem', 'TN', 'Tamil Nadu', 11.6643, 78.1460),
    ('Vellore', 'TN', 'Tamil Nadu', 12.9165, 79.1325),
    ('Guntur', 'AP', 'Andhra Pradesh', 16.3067, 80.4365),
    ('Nellore', 'AP', 'Andhra Pradesh', 14.4426, 79.9865),
    ('Tirupati', 'AP', 'Andhra Pradesh', 13.6288, 79.4192),
    ('Hubballi', 'KA', 'Karnataka', 15.3647, 75.1240),
    ('Belagavi', 'KA', 'Karnataka', 15.8497, 74.4977),
    ('Kozhikode', 'KL', 'Kerala', 11.2588, 75.7804),
    ('Thrissur', 'KL', 'Kerala', 10.5276, 76.2144),
    ('Aurangabad', 'MH', 'Maharashtra', 19.8762, 75.3433),
    ('Kolhapur', 'MH', 'Maharashtra', 16.7050, 74.2433),
    ('Jamshedpur', 'JH', 'Jharkhand', 22.8046, 86.2029),
    ('Prayagraj', 'UP', 'Uttar Pradesh', 25.4358, 81.8463),
    ('Meerut', 'UP', 'Uttar Pradesh', 28.9845, 77.7064),
    ('Gurugram', 'HR', 'Haryana', 28.4595, 77.0266),
    ('Noida', 'UP', 'Uttar Pradesh', 28.5355, 77.3910),
    ('Panaji', 'GA', 'Goa', 15.4909, 73.8278),
    ('Srinagar', 'JK', 'Jammu and Kashmir', 34.0837, 74.7973),
    ('Shimla', 'HP', 'Himachal Pradesh', 31.1048, 77.1734),
    ('Puducherry', 'PY', 'Puducherry', 11.9416, 79.8083),
    # The 63rd, paused (see PAUSED below).
    ('Siliguri', 'WB', 'West Bengal', 26.7271, 88.3953),
]

USA = [
    ('New York', 'NY', 'New York', 40.7128, -74.0060),
    ('Los Angeles', 'CA', 'California', 34.0522, -118.2437),
    ('Chicago', 'IL', 'Illinois', 41.8781, -87.6298),
    ('Houston', 'TX', 'Texas', 29.7604, -95.3698),
    ('Phoenix', 'AZ', 'Arizona', 33.4484, -112.0740),
    ('Philadelphia', 'PA', 'Pennsylvania', 39.9526, -75.1652),
    ('San Antonio', 'TX', 'Texas', 29.4241, -98.4936),
    ('San Diego', 'CA', 'California', 32.7157, -117.1611),
    ('Dallas', 'TX', 'Texas', 32.7767, -96.7970),
    ('San Jose', 'CA', 'California', 37.3382, -121.8863),
    ('Austin', 'TX', 'Texas', 30.2672, -97.7431),
    ('San Francisco', 'CA', 'California', 37.7749, -122.4194),
    ('Seattle', 'WA', 'Washington', 47.6062, -122.3321),
    ('Denver', 'CO', 'Colorado', 39.7392, -104.9903),
    ('Boston', 'MA', 'Massachusetts', 42.3601, -71.0589),
    ('Atlanta', 'GA', 'Georgia', 33.7490, -84.3880),
    ('Miami', 'FL', 'Florida', 25.7617, -80.1918),
    ('Washington', 'DC', 'District of Columbia', 38.9072, -77.0369),
    ('Las Vegas', 'NV', 'Nevada', 36.1699, -115.1398),
    ('Portland', 'OR', 'Oregon', 45.5152, -122.6784),
    ('Minneapolis', 'MN', 'Minnesota', 44.9778, -93.2650),
    ('Detroit', 'MI', 'Michigan', 42.3314, -83.0458),
    ('Charlotte', 'NC', 'North Carolina', 35.2271, -80.8431),
    ('Nashville', 'TN', 'Tennessee', 36.1627, -86.7816),
    ('Orlando', 'FL', 'Florida', 28.5383, -81.3792),
    ('Tampa', 'FL', 'Florida', 27.9506, -82.4572),
    ('Salt Lake City', 'UT', 'Utah', 40.7608, -111.8910),
    ('Columbus', 'OH', 'Ohio', 39.9612, -82.9988),
    ('Raleigh', 'NC', 'North Carolina', 35.7796, -78.6382),
    ('Kansas City', 'MO', 'Missouri', 39.0997, -94.5786),
    ('Pittsburgh', 'PA', 'Pennsylvania', 40.4406, -79.9959),
    ('Boise', 'ID', 'Idaho', 43.6150, -116.2023),
]

CANADA = [
    ('Toronto', 'ON', 'Ontario', 43.6532, -79.3832),
    ('Montreal', 'QC', 'Quebec', 45.5017, -73.5673),
    ('Vancouver', 'BC', 'British Columbia', 49.2827, -123.1207),
    ('Calgary', 'AB', 'Alberta', 51.0447, -114.0719),
    ('Edmonton', 'AB', 'Alberta', 53.5461, -113.4938),
    ('Ottawa', 'ON', 'Ontario', 45.4215, -75.6972),
    ('Winnipeg', 'MB', 'Manitoba', 49.8951, -97.1384),
    ('Quebec City', 'QC', 'Quebec', 46.8139, -71.2080),
    ('Hamilton', 'ON', 'Ontario', 43.2557, -79.8711),
    ('Halifax', 'NS', 'Nova Scotia', 44.6488, -63.5752),
    ('Victoria', 'BC', 'British Columbia', 48.4284, -123.3656),
]

UK = [
    ('London', 'ENG', 'England', 51.5074, -0.1278),
    ('Manchester', 'ENG', 'England', 53.4808, -2.2426),
    ('Birmingham', 'ENG', 'England', 52.4862, -1.8904),
    ('Glasgow', 'SCT', 'Scotland', 55.8642, -4.2518),
    ('Leeds', 'ENG', 'England', 53.8008, -1.5491),
    ('Liverpool', 'ENG', 'England', 53.4084, -2.9916),
    ('Edinburgh', 'SCT', 'Scotland', 55.9533, -3.1883),
    ('Bristol', 'ENG', 'England', 51.4545, -2.5879),
    ('Cardiff', 'WLS', 'Wales', 51.4816, -3.1791),
]

AUSTRALIA = [
    ('Sydney', 'NSW', 'New South Wales', -33.8688, 151.2093),
    ('Melbourne', 'VIC', 'Victoria', -37.8136, 144.9631),
    ('Brisbane', 'QLD', 'Queensland', -27.4698, 153.0251),
    ('Perth', 'WA', 'Western Australia', -31.9505, 115.8605),
    ('Adelaide', 'SA', 'South Australia', -34.9285, 138.6007),
]

# code -> (country name, city table, size multiplier, default tz, phone calling code)
COUNTRIES = {
    'IN': ('India', INDIA, 1.00, 'Asia/Kolkata', '91'),
    'US': ('United States', USA, 0.90, 'America/New_York', '1'),
    'CA': ('Canada', CANADA, 0.35, 'America/Toronto', '1'),
    'GB': ('United Kingdom', UK, 0.45, 'Europe/London', '44'),
    'AU': ('Australia', AUSTRALIA, 0.30, 'Australia/Sydney', '61'),
}

#: The last city of each of these tables is created `paused`.
PAUSED = {'IN': 'Siliguri', 'US': 'Boise'}

#: US/CA/AU state -> IANA zone where it differs from the country default.
STATE_TZ = {
    'CA': 'America/Los_Angeles', 'WA': 'America/Los_Angeles', 'OR': 'America/Los_Angeles',
    'NV': 'America/Los_Angeles', 'AZ': 'America/Phoenix', 'CO': 'America/Denver',
    'UT': 'America/Denver', 'ID': 'America/Boise', 'TX': 'America/Chicago',
    'IL': 'America/Chicago', 'MN': 'America/Chicago', 'MO': 'America/Chicago',
    'TN': 'America/Chicago',
    'BC': 'America/Vancouver', 'AB': 'America/Edmonton', 'MB': 'America/Winnipeg',
    'NS': 'America/Halifax',
    'VIC': 'Australia/Melbourne', 'QLD': 'Australia/Brisbane', 'WA_AU': 'Australia/Perth',
    'SA_AU': 'Australia/Adelaide',
}

# Real locality names for the ten big cities. (name, lat, lng)
LOCALITIES = {
    'Hyderabad': [
        ('Banjara Hills', 17.4126, 78.4482), ('Madhapur', 17.4483, 78.3915),
        ('HITEC City', 17.4435, 78.3772), ('Gachibowli', 17.4401, 78.3489),
        ('Kondapur', 17.4600, 78.3640), ('Jubilee Hills', 17.4325, 78.4071),
        ('Secunderabad', 17.4399, 78.4983), ('Kukatpally', 17.4948, 78.3996),
        ('Ameerpet', 17.4375, 78.4483), ('Begumpet', 17.4442, 78.4678),
    ],
    'Bengaluru': [
        ('Koramangala', 12.9352, 77.6245), ('Indiranagar', 12.9784, 77.6408),
        ('Whitefield', 12.9698, 77.7500), ('Jayanagar', 12.9308, 77.5838),
        ('HSR Layout', 12.9116, 77.6474), ('Electronic City', 12.8452, 77.6602),
        ('Malleshwaram', 13.0035, 77.5647), ('Hebbal', 13.0358, 77.5970),
    ],
    'Mumbai': [
        ('Bandra', 19.0596, 72.8295), ('Andheri', 19.1136, 72.8697),
        ('Powai', 19.1176, 72.9060), ('Colaba', 18.9067, 72.8147),
        ('Dadar', 19.0178, 72.8478), ('Juhu', 19.1075, 72.8263),
        ('Borivali', 19.2307, 72.8567), ('Navi Mumbai Vashi', 19.0771, 72.9986),
    ],
    'Delhi': [
        ('Connaught Place', 28.6315, 77.2167), ('Saket', 28.5245, 77.2066),
        ('Dwarka', 28.5921, 77.0460), ('Karol Bagh', 28.6519, 77.1909),
        ('Lajpat Nagar', 28.5677, 77.2433), ('Rohini', 28.7495, 77.0565),
        ('Vasant Kunj', 28.5200, 77.1590), ('Janakpuri', 28.6219, 77.0878),
    ],
    'Chennai': [
        ('T Nagar', 13.0418, 80.2341), ('Adyar', 13.0012, 80.2565),
        ('Velachery', 12.9815, 80.2180), ('Anna Nagar', 13.0850, 80.2101),
        ('OMR Sholinganallur', 12.9010, 80.2279), ('Guindy', 13.0067, 80.2206),
        ('Mylapore', 13.0368, 80.2676), ('Tambaram', 12.9249, 80.1000),
    ],
    'Pune': [
        ('Koregaon Park', 18.5362, 73.8940), ('Hinjewadi', 18.5912, 73.7389),
        ('Kothrud', 18.5074, 73.8077), ('Viman Nagar', 18.5679, 73.9143),
        ('Baner', 18.5590, 73.7868), ('Hadapsar', 18.5089, 73.9260),
        ('Shivajinagar', 18.5308, 73.8475), ('Wakad', 18.5987, 73.7603),
    ],
    'Kolkata': [
        ('Salt Lake', 22.5800, 88.4150), ('Park Street', 22.5535, 88.3520),
        ('Howrah', 22.5958, 88.2636), ('New Town', 22.5800, 88.4700),
        ('Ballygunge', 22.5276, 88.3650), ('Behala', 22.4986, 88.3118),
    ],
    'New York': [
        ('Midtown Manhattan', 40.7549, -73.9840), ('Brooklyn Heights', 40.6960, -73.9936),
        ('Williamsburg', 40.7081, -73.9571), ('Astoria', 40.7644, -73.9235),
        ('Upper West Side', 40.7870, -73.9754), ('Long Island City', 40.7447, -73.9485),
        ('Harlem', 40.8116, -73.9465), ('Flushing', 40.7675, -73.8330),
    ],
    'San Francisco': [
        ('Mission District', 37.7599, -122.4148), ('SoMa', 37.7785, -122.4056),
        ('Nob Hill', 37.7930, -122.4161), ('Sunset District', 37.7535, -122.4940),
        ('Marina District', 37.8030, -122.4370), ('Richmond District', 37.7800, -122.4800),
    ],
    'London': [
        ('Camden', 51.5390, -0.1426), ('Shoreditch', 51.5265, -0.0780),
        ('Canary Wharf', 51.5054, -0.0235), ('Kensington', 51.4990, -0.1938),
        ('Brixton', 51.4613, -0.1156), ('Islington', 51.5362, -0.1033),
        ('Greenwich', 51.4826, -0.0077), ('Hackney', 51.5450, -0.0553),
    ],
}

#: Share of trips starting in each local hour (0..23): two commute peaks, a quiet night.
HOUR_WEIGHTS = [
    1.2, 0.8, 0.6, 0.5, 0.7, 1.4, 2.6, 4.6, 6.2, 5.6, 4.4, 4.2,
    4.6, 4.4, 4.0, 4.2, 5.0, 6.4, 7.0, 6.2, 5.0, 3.8, 2.8, 1.8,
]

CANCEL_REASONS_RIDER = [
    'Changed my plans', 'Driver is taking too long', 'Booked by mistake',
    'Found another ride', 'Wrong pickup location',
]
CANCEL_REASONS_DRIVER = [
    'Rider not at pickup', 'Vehicle issue', 'Pickup too far', 'Personal reason',
]

VEHICLES = [
    ('Maruti Suzuki', 'Dzire', 'sedan'), ('Hyundai', 'Aura', 'sedan'),
    ('Toyota', 'Etios', 'sedan'), ('Tata', 'Tigor', 'sedan'),
    ('Maruti Suzuki', 'Swift', 'hatchback'), ('Hyundai', 'i10', 'hatchback'),
    ('Tata', 'Tiago', 'hatchback'), ('Toyota', 'Innova', 'suv'),
    ('Mahindra', 'Marazzo', 'suv'), ('Bajaj', 'RE Auto', 'auto'),
]
COLORS = ['White', 'Silver', 'Grey', 'Black', 'Blue', 'Red']

TICKET_ISSUES = [
    ('payment', 18), ('fare_dispute', 24), ('driver_behaviour', 14), ('rider_behaviour', 6),
    ('lost_item', 12), ('safety', 4), ('account', 10), ('app_bug', 8), ('other', 4),
]
