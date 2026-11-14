"""Provenance-aware location matching rules for voice Gaia2 tasks."""

# Values come from the reviewed 48-task location source audit.
VOICE_LOCATION_RULES = {
    "0076_ihkrjocj7n77vcaqjendy92rbmo05iqo": {
        "end_location": {"Meir 90, Leuven, Belgium": {"mode": "user"}}
    },
    "0097_kxccpkni1h796gejnt95f5yz1jottv2j": {
        "start_location": {
            "Fort Worth Flats, 9012 W 7th St, Fort Worth": {
                "environment_fragments": [
                    "9012 W 7th St, Fort Worth",
                    "Fort Worth Flats",
                ],
                "mode": "mixed",
            }
        }
    },
    "0098_kv0q6p0s0rtklxfz4udc4w8v1ae4qibk": {
        "end_location": {"34412 Office Rd, Cambridge": {"mode": "user"}},
        "location": {"Office Building": {"mode": "user"}},
    },
    "0105_8lf1buk2eflaicr58ite7st8v93dhr4y": {
        "end_location": {
            "Cavendish Laboratory, 19 JJ Thomson Ave, Cambridge CB3 0DY": {
                "environment_fragments": ["Cambridge CB3"],
                "mode": "mixed",
            }
        }
    },
    "0117_21kzyhlzscui6zvrqouyc1n2xuyhkbal": {
        "location": {"Zoom call": {"mode": "user"}}
    },
    "0128_i7kyxvttzakr8y8mc7jqmeo1loycy8jb": {
        "end_location": {
            "Frejgatan 103B, 113 26 Stockholm": {
                "environment_fragments": ["26 Stockholm"],
                "mode": "mixed",
            }
        }
    },
    "0141_2v7u2xwuricaq88si4purwtnp4sbo5fw": {
        "end_location": {
            "Travbaneplan 10, Solvalla, Hästsportens hus, 161 89 Stockholm, Sweden": {
                "mode": "user"
            }
        }
    },
    "0143_s99906zxfpwrfl5dnv983b1diotgpzoy": {
        "end_location": {"301 Cecelia Avenue": {"mode": "user"}},
        "location": {"Massive Makeover Beauty Spa": {"mode": "user"}},
    },
    "0147_knh0fssbzcq8a1lytctvobpulktdavgv": {
        "start_location": {
            "Gent-Sint-Pieters, 9000 Ghent": {
                "environment_fragments": ["9000 Ghent"],
                "mode": "mixed",
            }
        }
    },
    "0160_lz4oktw0o3l9wuwpuwn4x8c653cp1tr6": {
        "location": {"Hashbro Farmer's Market": {"mode": "user"}}
    },
    "0177_jqzi4bhuvix03mknnnjpoc6oduzbc174": {
        "location": {"Zoom": {"environment_fragments": ["Zoom"], "mode": "mixed"}}
    },
    "0187_wk3hw6dtnbrvzs94r1cjqmkwogkjziv6": {
        "location": {"Nottingham Park": {"mode": "user"}}
    },
    "0193_3spn1z5u8lotfe4gdqelx7yx3k8d25xu": {
        "location": {"Multiproduktion Norden AB": {"mode": "user"}}
    },
    "0383_c3eoypm9irwbeg9t4tdgk7w0gxeexu15": {
        "location": {"Alfredo's Pizza Café": {"mode": "user"}}
    },
    "0476_mmbz78x1qg2tl9tqomuzz7dbv8nsumdb": {
        "location": {"St. Michael's Hospital": {"mode": "user"}}
    },
    "0479_grlxsenm02c1hff3oqboxku2szsvuvn4": {
        "location": {"Portland Downtown Riverfront": {"mode": "user"}}
    },
    "0521_h3x8mxz2j3hsqikhyjvrycs08gt9mbth": {
        "end_location": {"4567 NW 24th Ave, Portland, OR 97210": {"mode": "user"}}
    },
    "0540_c7qrfnor6pea230wfdawbm2humo6clj1": {
        "location": {"Virtual": {"environment_fragments": ["Virtual"], "mode": "mixed"}}
    },
    "0581_3xlmfgx876o4l7hkn2tqkyvutiv7d32x": {
        "end_location": {
            "620 SW 5th Ave Suite 300, Portland, OR 97204": {"mode": "user"}
        }
    },
    "0610_ciifop2jmjyeph0bly3rqpeyz3t2b4lc": {
        "location": {"Virtual": {"environment_fragments": ["Virtual"], "mode": "mixed"}}
    },
    "0612_nxnlsgo0crpgkmib1mkfbcg744f1u09k": {
        "location": {"Zoom": {"environment_fragments": ["Zoom"], "mode": "mixed"}}
    },
    "0616_ewvwwn0b9kt8urb5oa6sh2ui5w1xi6tt": {
        "end_location": {"160 Rd Number 28B, Mumbai": {"mode": "user"}}
    },
    "0636_rmskjxrfmi05z4zftlr3li5tr96qxeas": {
        "end_location": {"1945 SE Water Ave, Portland, OR 97214": {"mode": "user"}}
    },
    "0644_2zmiyh1q3stoa9243ctp7ygwq87ffvkx": {
        "location": {
            "Local café": {"environment_fragments": ["Local café"], "mode": "mixed"},
            "Local café ": {"environment_fragments": ["Local café"], "mode": "mixed"},
        }
    },
    "0651_wj9ojjy0vyhg1x5ocia1s0828z2emu8n": {
        "end_location": {"133 Yoga Way, Kolkata": {"mode": "user"}}
    },
    "0656_3drd9ztjpzcq0jv0akdvfqfubkgwkove": {
        "end_location": {
            "Náměstí Míru 15": {
                "environment_fragments": ["Náměstí Míru 15"],
                "mode": "mixed",
            }
        }
    },
    "0666_2dsmofbug7ytmczfxa1a7y8xch0suuzh": {
        "end_location": {
            "Kungsholmsgatan 10, 112 27 Stockholm": {
                "environment_fragments": ["27 Stockholm"],
                "mode": "mixed",
            }
        }
    },
    "0675_t1zb3c21mvtsarrh5ndc890wfq4t618r": {
        "end_location": {
            "Avenue Louise 240, 1050 Brussels, Belgium": {
                "environment_fragments": ["1050 Brussels"],
                "mode": "mixed",
            }
        }
    },
    "0680_wmve2jrtqqqxwe54zykox0z7ipt59rgg": {
        "location": {"Pasta Caffee": {"environment_fragments": [], "mode": "user"}}
    },
    "0691_aw5sqbzfv1bjmj3vmxwpxmj0l3rx5br2": {
        "end_location": {
            "Czech Countryside, Štefánikova 646/76, 612 00 Brno-sever": {
                "environment_fragments": [
                    "646/76, 612 00 Brno-sever",
                    "Štefánikova 646",
                ],
                "mode": "mixed",
            }
        }
    },
    "0696_b39cceovqwq08978gues62vz510h1oko": {
        "location": {
            "Porto, Portugal": {
                "environment_fragments": ["Porto, Portugal"],
                "mode": "mixed",
            }
        }
    },
    "0698_pkyb3pflukov2hhnolhiv5cs9rfkf5tr": {
        "start_location": {
            " Karl Johans gate 31, 0159 Oslo": {
                "environment_fragments": [
                    "Karl Johans gate 31, 0159 Oslo",
                    "31, 0159 Oslo",
                    "Karl Johans gate",
                ],
                "mode": "mixed",
            }
        }
    },
    "0703_e6nz1ppwdudnjel0ojtznkulnfeqewot": {
        "end_location": {
            "Château de la Hulpe, Chaussée de Bruxelles 111, 1310 La Hulpe, Belgium": {
                "mode": "user"
            },
            "Irish College Leuven, Janseniusstraat 1, 3000 Leuven, Belgium": {
                "environment_fragments": ["1, 3000 Leuven", "3000 Leuven, Belgium"],
                "mode": "mixed",
            },
        }
    },
    "0719_pciarpzgm5cf0v9sp5ws2sm2m5uywxd1": {
        "location": {
            "Estate Tasting Room": {"environment_fragments": [], "mode": "user"}
        }
    },
    "0790_cljd6invhqwlhvau5ao73d3ger6j9oua": {
        "end_location": {
            "Sweden Arlanda Airport, 190 45 Stockholm": {
                "environment_fragments": ["190 45 Stockholm", "Arlanda Airport, 190"],
                "mode": "mixed",
            }
        },
        "start_location": {
            "Östermalmsgatan 12, 114 39 Stockholm": {
                "environment_fragments": [
                    "Östermalmsgatan 12, 114 39 Stockholm",
                    "12, 114 39 Stockholm",
                ],
                "mode": "mixed",
            }
        },
    },
    "0823_rwzr2hwaiw4ei864km2rhwdwgdv52zn2": {
        "end_location": {
            "Im Schmiedenhof 10, 4001 Basel": {
                "environment_fragments": ["10, 4001 Basel", "4001 Basel"],
                "mode": "mixed",
            }
        }
    },
    "0841_7oa5e611arhc1vl3nagzmxl27wgvo14c": {
        "location": {
            "Uppsala Castle": {
                "environment_fragments": ["Uppsala Castle"],
                "mode": "mixed",
            }
        }
    },
    "0851_uu3x2dagi180xcvws4uvkowpm6jpadpf": {
        "end_location": {
            "New Marine Lines, Marine Lines, Mumbai, Maharashtra 400020": {
                "environment_fragments": ["Mumbai, Maharashtra 400020"],
                "mode": "mixed",
            }
        }
    },
    "0859_0955qpz7w3j9ib9vml2osvr8fxjzswxs": {
        "location": {"Zoom": {"environment_fragments": ["Zoom"], "mode": "mixed"}}
    },
    "0865_uewqhv9xlg9bg9ggsfdnrecvgj8xcmkm": {
        "end_location": {
            "Božetěchova 5, 602 00 Brno, Czech Republic": {
                "environment_fragments": [
                    "5, 602 00 Brno, Czech Republic",
                    "Brno, Czech Republic",
                ],
                "mode": "mixed",
            }
        }
    },
    "0868_cr1csiqq8w6hm79fcof54tg3y038lbpt": {
        "location": {
            "Restaurant Office ": {
                "environment_fragments": ["Restaurant Office"],
                "mode": "mixed",
            }
        }
    },
    "0876_vhdis3c0sjtbyfig6jbhubt9eygu283b": {
        "end_location": {
            "Chhatrapati Shivaji Maharaj International Airport Mumbai": {
                "environment_fragments": [
                    "Chhatrapati Shivaji Maharaj International Airport Mumbai"
                ],
                "mode": "mixed",
            }
        }
    },
    "0890_p76cdsnqnnh13rpy12z7sj6f55ngiqdy": {
        "location": {"MS Teams": {"mode": "user"}}
    },
    "0921_92cwy9d3m4rlm3x8ifmm7fkey975fqkq": {
        "location": {
            "Antwerpen Bistro (Grote Markt 1, 2000 Antwerpen)": {
                "environment_fragments": ["1, 2000 Antwerpen"],
                "mode": "mixed",
            },
            "Antwerpen Bistro (Grote Markt 1, 2000 Antwerpen) ": {
                "environment_fragments": ["1, 2000 Antwerpen"],
                "mode": "mixed",
            },
        }
    },
    "0926_0ia7wtsv7hi4ifzdwanf230dp3egimqf": {
        "location": {"Main Conference Room": {"mode": "user"}}
    },
    "0929_gnj6ea1qobi8dn9joe3rf3p8zkj3fnge": {
        "location": {
            "København Resto (Østerbrogade 90, 2100 København Ø, Copenhagen)": {
                "environment_fragments": [
                    "90, 2100 København Ø, Copenhagen",
                    "2100 København Ø, Copenhagen",
                ],
                "mode": "mixed",
            }
        }
    },
    "0932_5jfayayraae5kkh471epw5qhdjb6c5sl": {
        "location": {"Bay Restaurant": {"mode": "user"}}
    },
    "0999_zvr6x3f73ec0cpjkhxwhuhdajj2chw4w": {
        "end_location": {"456 The Old Schools St.": {"mode": "user"}},
        "start_location": {"123 Spa Nature Rd.": {"mode": "user"}},
    },
}
